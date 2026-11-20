"""Run the gateway: `sre-gateway` (connect and serve) or `sre-gateway --check` (validate and exit)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
from collections.abc import Callable
from typing import Any

from sre_gateway import harness, pi, prompts
from sre_gateway.agent import AskPermission, SdkDriver
from sre_gateway.approvals import Decision
from sre_gateway.config import MODEL_ENV, PI_ENV, Config, ConfigError, load
from sre_gateway.gateway import Gateway, Settings
from sre_gateway.runner import NagiosFeed, Runner, RunnerSettings, RunnerState
from sre_gateway.tracker import GhTracker

log = logging.getLogger("sre_gateway")


def agent_options(  # pylint: disable=too-many-arguments
    config: Config, resume: str | None, can_use_tool: Any, triage: str | None = None, question: bool = False
) -> Any:
    """How every agent session runs: the site as working directory, the plugin loaded, only the chat policy.

    A triage-only session gets the narrower triage rules and instructions; the
    gateway starts it again with the chat ones once a responder speaks. A
    question gets the question rules and instructions, for life.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    options: dict[str, Any] = {
        "cwd": str(config.site_root),
        "add_dirs": [str(path) for path in config.readable_dirs()],
        "plugins": [{"type": "local", "path": str(harness.PLUGIN_ROOT)}],
        # No user, project or local settings: nothing on this machine can widen the policy.
        "setting_sources": [],
        "settings": harness.chat_policy.settings_json(config.chat, triage=bool(triage), question=question),
        "permission_mode": "default",
        "can_use_tool": can_use_tool,
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": prompts.system_append(str(harness.SCRIPTS), triage=bool(triage), question=question),
        },
        # A triage-only session carries its token: the harness scripts let it write only to the record it opened.
        "env": {
            "SRE_HARNESS_SITE": str(config.site_root),
            # One current-incident pointer serves every session: chat sessions must name their incident.
            harness.sre_common.NO_CURRENT_ENV: "1",
            **({harness.sre_common.TRIAGE_ENV: triage} if triage else {}),
        },
        "resume": resume,
        "stderr": lambda line: log.debug("agent: %s", line),
    }
    if config.mcp_config:
        options["mcp_servers"] = str(config.mcp_config)
    if config.model:
        options["model"] = config.model
    return ClaudeAgentOptions(**options)


def pi_problems(config: Config) -> list[str]:
    """Why the gateway cannot run Pi with this configuration; empty when it can."""
    problems = []
    if not config.model or "/" not in config.model:
        problems.append(f"{MODEL_ENV} must name the Pi model as provider/id")
    if not pi.find_pi(config.pi):
        problems.append(f"no Pi binary '{config.pi}' ({PI_ENV})")
    if config.runner and not config.runner_auth:
        problems.append("the runner has no read-only Nagios login for the agent's Nagios tools")
    return problems


def pi_driver_factory(config: Config) -> Callable[..., pi.PiDriver]:
    """Set up the gateway-owned Pi home, and return a factory for Pi sessions under the chat policy."""
    nagios = None
    base = config.nagios_base_url()
    if config.runner_auth and base:
        user, password = config.runner_auth
        nagios = {"nagios_url": base, "nagios_user": user, "nagios_pass": password}
    for warning in pi.prepare_home(config.pi_home, config.mcp_servers(), nagios, shutil.which(config.nagios_mcp)):
        log.warning("%s", warning)
    setup = pi.PiSetup(
        pi=pi.find_pi(config.pi) or config.pi,
        model=config.model or "",
        home=config.pi_home,
        session_dir=config.state_dir / "pi-sessions",
        cwd=config.site_root,
        env={"SRE_HARNESS_SITE": str(config.site_root), harness.sre_common.NO_CURRENT_ENV: "1"},
    )
    policies = {
        "chat": harness.tool_policy.Policy.from_rules(harness.chat_policy.rules(config.chat)),
        "triage": harness.tool_policy.Policy.from_rules(harness.chat_policy.triage_rules(config.chat)),
        "question": harness.tool_policy.Policy.from_rules(harness.chat_policy.question_rules(config.chat)),
    }

    def make(ask: AskPermission, triage: str | None, question: bool = False) -> pi.PiDriver:
        mode = "question" if question else "triage" if triage else "chat"
        return pi.PiDriver(
            setup,
            policies[mode],
            ask,
            system_append=prompts.system_append(str(harness.SCRIPTS), triage=bool(triage), question=question),
            triage_token=None if question else triage,
        )

    return make


def record(decision: Decision) -> None:
    """Write an approver's decision to the incident's audit log."""
    harness.audit.record_chat_decision(
        decision.incident,
        harness.audit.ChatDecision(
            outcome=decision.outcome,
            tool=decision.tool,
            tool_input=decision.tool_input,
            tool_use_id=decision.tool_use_id,
            approver=decision.approver,
            link=decision.link,
            reason=decision.reason,
        ),
    )


def build_runner(config: Config) -> Runner | None:
    """The alert source, when the site configures one (enabled or not: a drill needs it either way)."""
    if not config.runner:
        return None
    settings = RunnerSettings.from_site(config.doc)
    feed = NagiosFeed(config.site_root, config.doc, config.runner_auth)
    return Runner(settings, feed, RunnerState.load(config.state_dir / "runner.json"))


def build(config: Config) -> Gateway:
    from sre_gateway.slack import SlackAdapter  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    extra = dict(config.tokens)
    if config.runner_auth:
        extra["runner_password"] = config.runner_auth[1]
    secrets = harness.outbound_secrets(extra=extra, names=config.credential_names())

    def sdk_factory(ask: AskPermission, triage: str | None, question: bool = False) -> SdkDriver:
        return SdkDriver(lambda resume, can_use: agent_options(config, resume, can_use, triage, question), ask)

    driver_factory = pi_driver_factory(config) if config.agent == "pi" else sdk_factory

    settings = Settings(
        hub=config.hub,
        responders=config.responders,
        approvers=config.approvers,
        approval_ttl=config.approval_ttl,
        idle_seconds=config.idle_stop_seconds,
        max_sessions=config.max_sessions,
        state_dir=config.state_dir,
        question_idle_seconds=config.question_idle_seconds,
        attachments=config.attachments,
    )
    return Gateway(
        settings,
        chat=SlackAdapter(config.tokens["bot_token"], config.tokens["app_token"], config.link_hosts),
        tracker=GhTracker(config.doc),
        driver_factory=driver_factory,
        record=record,
        scrub=lambda text: harness.scrub(text, secrets),
        runner=build_runner(config),
        record_policy=harness.audit.record_policy_change,
        env_values=secrets,
        audit_closed=harness.audit_hook.is_closed,
    )


async def serve(config: Config) -> None:
    gateway = build(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(gateway.chat.stop()))
    await gateway.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat-ops gateway for the sre-harness plugin.")
    parser.add_argument("--check", action="store_true", help="validate the site and environment, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output, including the agent's stderr")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load()
        harness.chat_policy.rules(config.chat)
        problems = pi_problems(config) if config.agent == "pi" else []
        if problems:
            raise ConfigError("; ".join(problems))
    except (ConfigError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.check:
        runner = config.runner
        state = "none" if not runner else f"{'enabled' if runner['enabled'] else 'disabled'}, source {runner['source']}"
        print(
            f"ok: site {config.doc['site']['name']}, {len(config.responders)} responder(s), "
            f"{len(config.approvers)} approver(s), runner {state}, agent {config.agent} "
            f"(model {config.model or 'default'}), plugin {harness.PLUGIN_ROOT}"
        )
        return 0
    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
