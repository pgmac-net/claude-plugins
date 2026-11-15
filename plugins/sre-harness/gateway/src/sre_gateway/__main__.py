"""Run the gateway: `sre-gateway` (connect and serve) or `sre-gateway --check` (validate and exit)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from sre_gateway import harness, prompts
from sre_gateway.agent import AskPermission, SdkDriver
from sre_gateway.approvals import Decision
from sre_gateway.config import Config, ConfigError, load
from sre_gateway.gateway import Gateway, Settings
from sre_gateway.tracker import GhTracker

log = logging.getLogger("sre_gateway")


def agent_options(config: Config, resume: str | None, can_use_tool: Any) -> Any:
    """How every agent session runs: the site as working directory, the plugin loaded, only the chat policy."""
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    options: dict[str, Any] = {
        "cwd": str(config.site_root),
        "add_dirs": [str(path) for path in config.readable_dirs()],
        "plugins": [{"type": "local", "path": str(harness.PLUGIN_ROOT)}],
        # No user, project or local settings: nothing on this machine can widen the policy.
        "setting_sources": [],
        "settings": harness.chat_policy.settings_json(config.chat),
        "permission_mode": "default",
        "can_use_tool": can_use_tool,
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": prompts.system_append(str(harness.SCRIPTS)),
        },
        "env": {"SRE_HARNESS_SITE": str(config.site_root)},
        "resume": resume,
        "stderr": lambda line: log.debug("agent: %s", line),
    }
    if config.mcp_config:
        options["mcp_servers"] = str(config.mcp_config)
    if config.model:
        options["model"] = config.model
    return ClaudeAgentOptions(**options)


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


def build(config: Config) -> Gateway:
    from sre_gateway.slack import SlackAdapter  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    secrets = harness.outbound_secrets(extra=config.tokens, names=config.credential_names())

    def driver_factory(ask: AskPermission) -> SdkDriver:
        return SdkDriver(lambda resume, can_use: agent_options(config, resume, can_use), ask)

    settings = Settings(
        hub=config.hub,
        responders=config.responders,
        approvers=config.approvers,
        approval_ttl=config.approval_ttl,
        idle_seconds=config.idle_stop_seconds,
        max_sessions=config.max_sessions,
        state_dir=config.state_dir,
    )
    return Gateway(
        settings,
        chat=SlackAdapter(config.tokens["bot_token"], config.tokens["app_token"]),
        tracker=GhTracker(config.doc),
        driver_factory=driver_factory,
        record=record,
        scrub=lambda text: harness.scrub(text, secrets),
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
    except (ConfigError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(
            f"ok: site {config.doc['site']['name']}, {len(config.responders)} responder(s), "
            f"{len(config.approvers)} approver(s), plugin {harness.PLUGIN_ROOT}"
        )
        return 0
    asyncio.run(serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
