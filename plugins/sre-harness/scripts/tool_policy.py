"""Judge one tool call against a chat policy's rule lists: allow, deny or ask.

Claude Code evaluates the gateway's permission rules itself. An agent with no
permission system (Pi) sends every call to the gateway, which judges it here
against the same lists that chat_policy builds for Claude Code.

This matcher errs strict. It is a security boundary, and when it cannot be sure
a call is covered by an allow rule, the answer is "ask", which becomes an
approval request (or a refusal in a triage-only session):

- A shell command is allowed only when it is one plain command: no chaining,
  pipe, redirect or background operator, no substitution or variable expansion
  (any `$` or backtick), no newline. It must start with an allow rule's prefix.
- Deny rules are checked first, against the whole command and each simple
  command within it, and a match anywhere refuses the call.
- A Read deny rule also covers editing and writing the same path.
- Paths are made absolute and resolved through symlinks before matching.
- A recursive search (Grep, Glob) whose root contains a denied directory asks
  rather than being allowed, so searching from a parent cannot read inside it.
- Claude Code's own built-in read-only shell commands are not allowed here.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import audit

ALLOW, DENY, ASK = "allow", "deny", "ask"

# Pi's built-in tools under the names the rules use.
PI_TOOL_NAMES = {
    "bash": "Bash",
    "read": "Read",
    "grep": "Grep",
    "find": "Glob",
    "ls": "Glob",
    "edit": "Edit",
    "write": "Write",
}
PATH_KEYS = ("file_path", "path", "notebook_path")
SEARCH_TOOLS = frozenset({"Grep", "Glob"})
WRITE_TOOLS = frozenset({"Edit", "Write"})
# Anything that makes one shell command do more than run one program with literal arguments.
UNSAFE_SHELL = re.compile(r"[`$\n\r]")
OPERATOR_CHARS = frozenset(";&|<>()")
RULE = re.compile(r"^(?P<tool>[A-Za-z_][\w-]*)(?:\((?P<content>.*)\))?$", re.DOTALL)


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str


@dataclass(frozen=True)
class Rule:
    tool: str
    content: str | None  # None: the whole tool


def parse(rule: str) -> Rule:
    match = RULE.match(rule.strip())
    if not match:
        raise ValueError(f"not a permission rule: {rule!r}")
    return Rule(match.group("tool"), match.group("content"))


@dataclass
class Policy:
    """A parsed rule set: what runs without asking, and what never runs."""

    allow: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)
    home: Path = field(default_factory=Path.home)

    @classmethod
    def from_rules(cls, rules: dict[str, list[str]], home: Path | None = None) -> Policy:
        return cls(
            allow=[parse(r) for r in rules.get("allow", [])],
            deny=[parse(r) for r in rules.get("deny", [])],
            home=home or Path.home(),
        )


def canonical_tool(name: str) -> str:
    """The rule-language name of a tool: Pi's built-ins are mapped, everything else is kept."""
    return PI_TOOL_NAMES.get(name, name)


# --- shell commands ---------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A Bash rule with '*' inside: '*' matches anything, including spaces."""
    return re.compile("".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern)))


def _bash_matches(content: str, command: str) -> bool:
    """Whether one command matches a Bash rule's content, the way Claude Code reads it."""
    if content.endswith(" *") and "*" not in content[:-2]:
        prefix = content[:-2]
        return command == prefix or command.startswith(prefix + " ")
    if "*" in content:
        return bool(_glob_regex(content).fullmatch(command))
    return command == content


def _simple_commands(command: str) -> list[str] | None:
    """Each simple command in a shell line, rebuilt from its words; None if it cannot be parsed."""
    segments = audit.command_segments(command)
    return None if segments is None else [" ".join(words) for words in segments]


def _has_operator(command: str) -> bool:
    """Whether any unquoted shell operator appears, even one that leaves a single command ('cmd &')."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return any(token and set(token) <= OPERATOR_CHARS for token in lexer)
    except ValueError:
        return True


def _shape_problem(command: str, parts: list[str] | None) -> str | None:
    """Why a command is more than one plain program with literal arguments; None if it is not."""
    if not command:
        return "empty command"
    if UNSAFE_SHELL.search(command):
        return "the command expands variables, substitutes commands or spans lines"
    if parts is None or len(parts) != 1 or _has_operator(command):
        return "not one plain command: chaining, pipes, redirects or backgrounding need approval"
    return None


def _judge_bash(policy: Policy, command: str) -> Verdict:
    command = command.strip()
    parts = _simple_commands(command)
    candidates = [command, *(parts or [])]
    for rule in policy.deny:
        if rule.tool == "Bash" and rule.content is not None:
            if any(_bash_matches(rule.content, candidate) for candidate in candidates):
                return Verdict(DENY, f"denied by Bash({rule.content})")
    problem = _shape_problem(command, parts)
    if problem:
        return Verdict(ASK, problem)
    for rule in policy.allow:
        if rule.tool == "Bash":
            if rule.content is None:
                return Verdict(ALLOW, "allowed by Bash")
            if _bash_matches(rule.content, command):
                return Verdict(ALLOW, f"allowed by Bash({rule.content})")
    return Verdict(ASK, "no allow rule covers this command")


# --- paths ------------------------------------------------------------------------


def _path_regex(pattern: str, home: Path) -> re.Pattern[str]:
    """A Read/Edit path rule as a regex over absolute paths: '**' any depth, '*' within one name."""
    if pattern.startswith("~/"):
        pattern = str(home) + pattern[1:]
    elif pattern.startswith("//"):
        pattern = pattern[1:]
    out = []
    for piece in re.split(r"(\*\*/?|\*|\?)", pattern):
        if piece in ("**", "**/"):
            out.append(".*")
        elif piece == "*":
            out.append("[^/]*")
        elif piece == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(piece))
    regex = "".join(out)
    # A pattern that does not start at the root ('**/.env') may sit under any directory.
    return re.compile(regex if pattern.startswith("/") else f"(?:.*/)?{regex}")


def _literal_root(pattern: str, home: Path) -> Path | None:
    """The fixed directory a path rule lives under ('~/.ssh/**' -> ~/.ssh); None if it can be anywhere."""
    if pattern.startswith("~/"):
        pattern = str(home) + pattern[1:]
    elif pattern.startswith("//"):
        pattern = pattern[1:]
    if not pattern.startswith("/"):
        return None
    fixed = re.split(r"[*?]", pattern, maxsplit=1)[0]
    return Path(fixed.rstrip("/") or "/") if "/" in fixed else None


def _glob_root(pattern: str) -> str | None:
    """The fixed directory an escaping glob pattern starts from ('/etc/*.conf' -> '/etc'); None if it stays local."""
    if not (pattern.startswith(("/", "~")) or ".." in pattern.split("/")):
        return None
    fixed = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
    return fixed.rsplit("/", 1)[0] or "/" if "/" in fixed else fixed


def _target(tool_input: dict[str, Any], cwd: Path) -> Path:
    """The path a file tool acts on, absolute and with symlinks resolved; the working directory if none.

    A glob pattern that is absolute or climbs out ('..') names where the search really starts.
    """
    raw = next((tool_input[key] for key in PATH_KEYS if isinstance(tool_input.get(key), str) and tool_input[key]), None)
    pattern = tool_input.get("pattern")
    escaping = _glob_root(pattern) if isinstance(pattern, str) else None
    if escaping is not None:
        base = Path(os.path.expanduser(raw)) if raw else cwd
        raw = str(Path(os.path.expanduser(escaping)) if escaping.startswith(("/", "~")) else base / escaping)
    path = Path(os.path.expanduser(raw)) if raw else cwd
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.realpath(path))


def _path_matches(rule: Rule, path: Path, home: Path) -> bool:
    if rule.content is None:
        return True
    regex = _path_regex(rule.content, home)
    text = str(path)
    return bool(regex.fullmatch(text) or regex.fullmatch(text + "/"))


def _judge_path_tool(policy: Policy, tool: str, tool_input: dict[str, Any], cwd: Path) -> Verdict:
    path = _target(tool_input, cwd)
    # Read rules protect a path from editing too; Edit rules only from editing.
    guarded_by = {"Read", "Edit"} if tool in WRITE_TOOLS else {"Read"}
    for rule in policy.deny:
        if rule.tool in guarded_by or rule.tool == tool:
            if rule.content is None and rule.tool != tool:
                continue
            if _path_matches(rule, path, policy.home):
                return Verdict(DENY, f"denied by {rule.tool}({rule.content or ''})")
            root = _literal_root(rule.content or "", policy.home) if tool in SEARCH_TOOLS else None
            if root is not None and (root == path or path in root.parents):
                return Verdict(ASK, f"the search covers {root}, which is denied")
    for rule in policy.allow:
        if rule.tool == tool and _path_matches(rule, path, policy.home):
            return Verdict(ALLOW, f"allowed by {tool}{'' if rule.content is None else f'({rule.content})'}")
    return Verdict(ASK, f"no allow rule covers {tool} on {path}")


# --- any tool ---------------------------------------------------------------------


def decide(policy: Policy, tool: str, tool_input: dict[str, Any], cwd: Path) -> Verdict:
    """allow, deny or ask for one call. tool may be a Pi name ('bash') or a rule name ('Bash')."""
    name = canonical_tool(tool)
    if name == "Bash":
        return _judge_bash(policy, str(tool_input.get("command", "")))
    if name in ("Read", "Edit", "Write", "Grep", "Glob"):
        return _judge_path_tool(policy, name, tool_input, cwd)
    for rule in policy.deny:
        if rule.tool == name and rule.content is None:
            return Verdict(DENY, f"denied by {name}")
    for rule in policy.allow:
        if rule.tool == name and rule.content is None:
            return Verdict(ALLOW, f"allowed by {name}")
    return Verdict(ASK, f"no allow rule names {name}")
