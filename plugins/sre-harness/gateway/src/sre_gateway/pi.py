"""The driver that runs Pi in RPC mode behind the gateway (ADR 0021).

Pi has no permission system. Its sre-policy extension sends every tool call
here as a blocking extension dialog; this driver judges it with the strict
matcher against the same rules Claude Code gets, and only what the rules leave
open goes to the gateway's approval flow (or is refused in a triage-only
session). The answer goes back to Pi, which runs or blocks the call.

Pi runs from a gateway-owned home (PI_CODING_AGENT_DIR), so nothing from the
user's own Pi set-up reaches the agent, and with only the harness's skills and
extensions. MCP tools are exposed directly and codemode is off, so every call
the policy sees is one tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sre_gateway import harness
from sre_gateway.agent import AskPermission, Event, Notice, Text, ToolCall, ToolOutput, TurnEnd

log = logging.getLogger(__name__)

POLICY_TITLE = "sre-harness-policy/v1"
TOOLS = "read,bash,grep,find,ls,edit,write"
EXCLUDED_TOOLS = "codemode,tool_search"
DIALOGS = frozenset({"select", "confirm", "input", "editor"})
LINE_LIMIT = 16 * 1024 * 1024
STOP_SECONDS = 10.0
COMMAND_SECONDS = 60.0
ADAPTERS = harness.PLUGIN_ROOT / "adapters" / "pi"


@dataclass
class PiSetup:
    """Everything that stays the same for every Pi session of one gateway."""

    pi: str
    model: str
    home: Path
    session_dir: Path
    cwd: Path
    plugin_root: Path = field(default_factory=lambda: harness.PLUGIN_ROOT)
    env: dict[str, str] = field(default_factory=dict)

    def command(self, system_append: str, resume: str | None) -> list[str]:
        argv = [
            self.pi,
            "--mode",
            "rpc",
            "--model",
            self.model,
            "--session-dir",
            str(self.session_dir),
            "--no-approve",
            "--no-skills",
            "--skill",
            str(self.plugin_root / "skills"),
            "--no-context-files",
            "--no-prompt-templates",
            "--extension",
            str(self.plugin_root / "adapters" / "pi" / "sre-audit.ts"),
            "--extension",
            str(self.plugin_root / "adapters" / "pi" / "sre-policy.ts"),
            "--tools",
            TOOLS,
            "--exclude-tools",
            EXCLUDED_TOOLS,
            "--append-system-prompt",
            system_append,
        ]
        if resume:
            argv += ["--session", resume]
        return argv


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(data, encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)


def prepare_home(
    home: Path, mcp_servers: dict[str, dict[str, Any]], nagios: dict[str, str] | None, nagios_command: str | None
) -> list[str]:
    """Write the gateway-owned Pi home's MCP set-up. Returns warnings about servers Pi cannot use.

    Each stdio or HTTP server from the gateway's MCP configuration is exposed directly; SSE
    servers are skipped, as Pi cannot speak SSE. Nagios is a local stdio server reading Nagios
    with the given login, whose config file only this user can read.
    """
    warnings = []
    servers: dict[str, dict[str, Any]] = {}
    for name, spec in mcp_servers.items():
        if name == "nagios" and nagios is not None:
            continue
        if spec.get("type") == "sse":
            warnings.append(f"MCP server '{name}' speaks only SSE, which Pi cannot use; it is left out")
            continue
        if "command" in spec or "url" in spec:
            servers[name] = {**{k: v for k, v in spec.items() if k != "type"}, "exposure": "direct"}
    if nagios is not None and nagios_command:
        config = home / "nagios-mcp.json"
        _write_private(config, json.dumps(nagios) + "\n")
        servers["nagios"] = {
            "command": nagios_command,
            "args": ["--transport", "stdio", "--config", str(config)],
            "exposure": "direct",
        }
    elif nagios is not None:
        warnings.append("no nagios-mcp command: the agent has no Nagios tools")
    _write_private(home / "mcp.json", json.dumps({"autoEnableCodemode": False, "mcpServers": servers}, indent=2))
    if not (home / "settings.json").exists():
        _write_private(home / "settings.json", "{}\n")
    return warnings


def find_pi(explicit: str | None = None) -> str | None:
    return explicit if explicit and Path(explicit).is_file() else shutil.which(explicit or "pi")


def _result_text(result: Any) -> str:
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return "" if result is None else str(result)
    return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))


ERRORS = {
    "rate limit": "The agent hit its usage limit. It will continue when the limit resets; send a message then.",
    "usage limit": "The agent hit its usage limit. It will continue when the limit resets; send a message then.",
    "401": "The agent could not sign in. The Pi provider login in the gateway's Pi home needs renewing.",
    "403": "The agent's provider refused the request (403). Check the provider subscription or login.",
}


def _explain(error: str) -> str:
    lowered = error.lower()
    return next((text for key, text in ERRORS.items() if key in lowered), error)


class PiDriver:  # pylint: disable=too-many-instance-attributes
    """Runs one Pi session in RPC mode."""

    def __init__(  # noqa: PLR0913  pylint: disable=too-many-arguments
        self,
        setup: PiSetup,
        policy: Any,
        ask: AskPermission,
        *,
        system_append: str,
        triage_token: str | None = None,
    ):
        self._setup = setup
        self._policy = policy
        self._ask = ask
        self._system_append = system_append
        self._triage_token = triage_token
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._events: asyncio.Queue = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._dialogs: set[asyncio.Task] = set()
        self.session_id: str | None = None

    # --- lifecycle -------------------------------------------------------------

    async def start(self, resume: str | None) -> None:
        env = {**os.environ, **self._setup.env, "PI_CODING_AGENT_DIR": str(self._setup.home)}
        env.update({"PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"})
        if self._triage_token:
            env[harness.sre_common.TRIAGE_ENV] = self._triage_token
        else:
            env.pop(harness.sre_common.TRIAGE_ENV, None)
        self._process = await asyncio.create_subprocess_exec(
            *self._setup.command(self._system_append, resume),
            cwd=str(self._setup.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
        )
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._drain_stderr())
        state = await self._command({"type": "get_state"})
        if not state.get("success"):
            raise RuntimeError(f"Pi did not start: {state.get('error', 'no state')}")
        self.session_id = (state.get("data") or {}).get("sessionId") or resume

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        for task in list(self._dialogs):
            task.cancel()
        with contextlib.suppress(Exception):
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), STOP_SECONDS)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_SECONDS)
            except TimeoutError:
                process.kill()
        if self._reader:
            self._reader.cancel()

    # --- a turn ------------------------------------------------------------------

    async def turn(self, message: str) -> AsyncIterator[Event]:
        if self._process is None:
            raise RuntimeError("the agent session is not started")
        error: str | None = None
        reply = await self._command({"type": "prompt", "message": message})
        if not reply.get("success"):
            yield TurnEnd(self.session_id, f"Pi refused the message: {reply.get('error', 'unknown error')}")
            return
        if (reply.get("data") or {}).get("disposition") == "handled":
            yield TurnEnd(self.session_id)
            return
        while True:
            record = await self._events.get()
            if record is None:
                yield TurnEnd(self.session_id, error or "the agent stopped without finishing its turn")
                return
            kind = record.get("type")
            if kind == "agent_settled":
                if record.get("aborted") and not error:
                    error = "the turn was aborted"
                yield TurnEnd(self.session_id, error)
                return
            events, problem = self._translate(record)
            error = problem or error
            for event in events:
                yield event

    def _translate(self, record: dict[str, Any]) -> tuple[list[Event], str | None]:
        """The gateway's events for one Pi event, and an error it reports, if any."""
        kind = record.get("type")
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") != "assistant":
                return [], None
            events: list[Event] = [
                Text(part["text"])
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text" and str(part.get("text", "")).strip()
            ]
            failure = message.get("errorMessage") if message.get("stopReason") == "error" else None
            if failure:
                events.append(Notice(_explain(str(failure))))
            return events, str(failure) if failure else None
        if kind == "tool_execution_start":
            name = harness.tool_policy.canonical_tool(str(record.get("toolName", "")))
            return [ToolCall(str(record.get("toolCallId", "")), name, dict(record.get("args") or {}))], None
        if kind == "tool_execution_end":
            text = _result_text(record.get("result"))
            return [ToolOutput(str(record.get("toolCallId", "")), text, bool(record.get("isError")))], None
        if kind == "extension_error":
            return [Notice(f"A Pi extension failed in {record.get('event')}: {record.get('error')}")], None
        if kind == "auto_retry_end" and not record.get("success"):
            final = str(record.get("finalError", "the provider kept failing"))
            return [Notice(_explain(final))], final
        return [], None

    # --- the protocol --------------------------------------------------------------

    async def _send(self, record: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("the agent session is not running")
        async with self._write_lock:
            process.stdin.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")
            await process.stdin.drain()

    async def _command(self, record: dict[str, Any]) -> dict[str, Any]:
        command_id = f"gw-{next(self._ids)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        try:
            await self._send({**record, "id": command_id})
            return await asyncio.wait_for(future, COMMAND_SECONDS)
        finally:
            self._pending.pop(command_id, None)

    async def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None  # noqa: S101 - started above
        try:
            while line := await process.stdout.readline():
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    log.warning("Pi wrote a line that is not JSON; ignored")
                    continue
                if isinstance(record, dict):
                    self._dispatch(record)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_result({"success": False, "error": "the agent exited"})
            await self._events.put(None)

    def _dispatch(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "response":
            future = self._pending.get(str(record.get("id")))
            if future is not None and not future.done():
                future.set_result(record)
        elif kind == "extension_ui_request":
            if record.get("method") not in DIALOGS:
                return
            task = asyncio.create_task(self._answer(record))
            self._dialogs.add(task)
            task.add_done_callback(self._dialogs.discard)
        else:
            self._events.put_nowait(record)

    async def _answer(self, request: dict[str, Any]) -> None:
        """Answer one dialog: a policy question gets a decision; any other dialog is declined."""
        request_id = request.get("id")
        if request.get("method") != "input" or request.get("title") != POLICY_TITLE:
            await self._send({"type": "extension_ui_response", "id": request_id, "cancelled": True})
            return
        decision = await self._decide(str(request.get("placeholder", "")))
        await self._send({"type": "extension_ui_response", "id": request_id, "value": json.dumps(decision)})

    async def _decide(self, payload: str) -> dict[str, str]:
        try:
            call = json.loads(payload)
            tool, tool_input, call_id = str(call["tool"]), dict(call.get("input") or {}), str(call["toolCallId"])
        except (ValueError, KeyError, TypeError):
            return {"decision": "deny", "reason": "the call could not be read"}
        if not call_id:
            return {"decision": "deny", "reason": "the call has no id, so an approval could not be bound to it"}
        verdict = harness.tool_policy.decide(self._policy, tool, tool_input, self._setup.cwd)
        if verdict.decision == harness.tool_policy.ALLOW:
            return {"decision": "allow", "reason": verdict.reason}
        if verdict.decision == harness.tool_policy.DENY:
            return {"decision": "deny", "reason": verdict.reason}
        # Asked under the rule-language name, so the approval and the audit hook's attempt name the same tool.
        answer = await self._ask(harness.tool_policy.canonical_tool(tool), tool_input, call_id)
        return {"decision": "allow" if answer.allow else "deny", "reason": answer.message or verdict.reason}

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            log.debug("pi: %s", line.decode("utf-8", "replace").rstrip())
