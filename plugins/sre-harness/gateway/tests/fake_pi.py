"""A stand-in for `pi --mode rpc`: speaks the JSONL protocol, scripted by the prompt text.

Prompts:
- "hello": one assistant reply.
- "tool <name> <json input>": asks the policy dialog for that call, then runs or blocks it.
- "two tools": two policy dialogs in flight at once, answered in reverse order is fine.
- "confirm": an ordinary (non-policy) confirm dialog; reports whether it was cancelled.
- "error": a provider error (usage limit).
- "garbage": writes a non-JSON line and a non-object, then replies.
- "crash": exits in the middle of the turn.
Anything else is echoed back.

FAKE_PI_LOG names a file that gets one JSON line per start: argv, cwd, and the
triage token if set.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

POLICY_TITLE = "sre-harness-policy/v1"
SESSION_DIR_FLAG = "--session-dir"


def send(record: object) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


class Fake:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.session = argv[argv.index("--session") + 1] if "--session" in argv else f"s-{uuid.uuid4().hex[:8]}"
        self.dialogs = 0
        self.inbox: list[dict] = []

    def read(self) -> dict | None:
        if self.inbox:
            return self.inbox.pop(0)
        line = sys.stdin.readline()
        return json.loads(line) if line else None

    def wait_answer(self, dialog_id: str) -> dict:
        """Read until the response to this dialog arrives; keep anything else for later."""
        while True:
            line = sys.stdin.readline()
            if not line:
                sys.exit(0)
            record = json.loads(line)
            if record.get("type") == "extension_ui_response" and record.get("id") == dialog_id:
                return record
            self.inbox.append(record)

    def ask_policy(self, call_id: str, tool: str, tool_input: dict) -> str:
        self.dialogs += 1
        dialog_id = f"d-{self.dialogs}"
        payload = json.dumps({"toolCallId": call_id, "tool": tool, "input": tool_input, "parentToolCallId": None})
        send(
            {
                "type": "extension_ui_request",
                "id": dialog_id,
                "method": "input",
                "title": POLICY_TITLE,
                "placeholder": payload,
            }
        )
        return self.decision(self.wait_answer(dialog_id))

    @staticmethod
    def decision(answer: dict) -> str:
        if answer.get("cancelled") or not isinstance(answer.get("value"), str):
            return "blocked: no answer"
        verdict = json.loads(answer["value"])
        return "ran" if verdict.get("decision") == "allow" else f"blocked: {verdict.get('reason')}"

    def run_tool(self, call_id: str, tool: str, tool_input: dict, outcome: str) -> None:
        if outcome == "ran":
            send({"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": tool_input})
            result = {"content": [{"type": "text", "text": f"output of {tool}"}]}
            send(
                {
                    "type": "tool_execution_end",
                    "toolCallId": call_id,
                    "toolName": tool,
                    "result": result,
                    "isError": False,
                }
            )
        else:
            result = {"content": [{"type": "text", "text": outcome}]}
            send(
                {
                    "type": "tool_execution_end",
                    "toolCallId": call_id,
                    "toolName": tool,
                    "result": result,
                    "isError": True,
                }
            )

    @staticmethod
    def reply(text: str, **extra: object) -> None:
        message = {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "stop", **extra}
        send({"type": "message_end", "message": message})

    @staticmethod
    def settle() -> None:
        send({"type": "agent_end", "messages": [], "willRetry": False})
        send({"type": "agent_settled", "aborted": False})

    def prompt(self, text: str) -> None:  # noqa: C901, PLR0912  pylint: disable=too-many-branches
        send({"type": "agent_start"})
        send({"type": "message_end", "message": {"role": "user", "content": text}})
        if text == "hello":
            self.reply("Hello from Pi.")
        elif text.startswith("tool "):
            _, tool, raw = text.split(" ", 2)
            outcome = self.ask_policy("call-1", tool, json.loads(raw))
            self.run_tool("call-1", tool, json.loads(raw), outcome)
            self.reply(outcome)
        elif text == "two tools":
            # Both dialogs open at once, as when Pi runs tool calls in parallel.
            for dialog_id, call_id, command in (("d-a", "call-a", "date"), ("d-b", "call-b", "rm -rf /tmp/x")):
                payload = json.dumps({"toolCallId": call_id, "tool": "bash", "input": {"command": command}})
                send(
                    {
                        "type": "extension_ui_request",
                        "id": dialog_id,
                        "method": "input",
                        "title": POLICY_TITLE,
                        "placeholder": payload,
                    }
                )
            answers: dict[str, dict] = {}
            while len(answers) < 2:
                line = sys.stdin.readline()
                if not line:
                    sys.exit(0)
                record = json.loads(line)
                answers[record["id"]] = record
            self.reply(f"{self.decision(answers['d-a'])} / {self.decision(answers['d-b'])}")
        elif text == "confirm":
            send({"type": "extension_ui_request", "id": "c-1", "method": "confirm", "title": "Trust?", "message": "x"})
            answer = self.wait_answer("c-1")
            self.reply("cancelled" if answer.get("cancelled") else "confirmed")
        elif text == "error":
            self.reply("", stopReason="error", errorMessage="429 rate limit exceeded")
        elif text == "garbage":
            sys.stdout.write("this is not json\n[1, 2]\n")
            sys.stdout.flush()
            self.reply("still here")
        elif text == "crash":
            sys.exit(3)
        else:
            self.reply(f"echo: {text}")
        self.settle()

    def serve(self) -> None:
        while (record := self.read()) is not None:
            kind, command_id = record.get("type"), record.get("id")
            if kind == "get_state":
                data = {"sessionId": self.session, "sessionFile": f"/sessions/{self.session}.jsonl"}
                send({"type": "response", "id": command_id, "command": kind, "success": True, "data": data})
            elif kind == "prompt":
                send(
                    {
                        "type": "response",
                        "id": command_id,
                        "command": kind,
                        "success": True,
                        "data": {"disposition": "started"},
                    }
                )
                self.prompt(record["message"])
            else:
                send({"type": "response", "id": command_id, "command": kind, "success": False, "error": "unknown"})


def main() -> None:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_PI_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            entry = {
                "argv": argv,
                "cwd": os.getcwd(),
                "triage": os.environ.get("SRE_HARNESS_TRIAGE"),
                "home": os.environ.get("PI_CODING_AGENT_DIR"),
            }
            handle.write(json.dumps(entry) + "\n")
    Fake(argv).serve()


if __name__ == "__main__":
    main()
