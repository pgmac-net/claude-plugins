"""Tool policy for an agent driven from chat.

The chat gateway runs the agent under Claude Code's own permission system.
A tool call that matches an allow rule runs at once; one that matches a deny
rule is refused; anything else becomes an approval request in chat. This
module holds the plugin's default rules and merges a site's additions.

Order of evaluation is Claude Code's: deny, then ask, then allow. A site can
add allow rules but never remove a deny rule, and an allow rule that would
let a mutation, a whole program or a whole tool through without approval is
refused.

Not a sandbox. Claude Code treats a fixed set of read-only shell commands
(cat, grep, ls, read-only git and similar) as allowed in every mode, and
applies Read deny rules to them only on a best-effort basis. The deny-list
narrows what an agent can reach; the approval gate is what stops a change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import audit

SCRIPTS = Path(__file__).resolve().parent

# Harness scripts the agent may run without asking, as '<script> [<subcommand>]'.
# Each becomes 'Bash(uv run <absolute path> [<subcommand>] *)'. Tracker writes
# (timeline entries, the issue body) are record-keeping, not system changes.
# 'audit.py record' is left out on purpose: under chat-ops only the gateway
# records approvals, so the agent cannot credit its own mutation.
HARNESS_CALLS = (
    "incident.py open",
    "incident.py attach",
    "incident.py status",
    "incident.py set",
    "incident.py close",
    "timeline.py",
    "audit.py show",
    "audit.py verify",
    "autonomy.py match",
    "autonomy.py run",
    "assist.py",
    "validate_site.py",
    "catalog_check.py",
)

# Read-only commands beyond Claude Code's built-in set. Nothing here takes a
# file argument that could name a credential outside the Read deny rules.
READ_ONLY_BASH = (
    "kubectl get",
    "kubectl describe",
    "kubectl logs",
    "kubectl top",
    "kubectl events",
    "kubectl api-resources",
    "kubectl version",
    "kubectl config current-context",
    "kubectl config get-contexts",
    "gh issue view",
    "gh issue list",
    "gh pr view",
    "gh pr list",
    "git log",
    "git show",
    "git diff",
    "git status",
    "journalctl",
    "systemctl status",
    "systemctl is-active",
    "systemctl list-units",
    "date",
    "uptime",
)

READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "Skill", "TodoWrite")

# Paths no agent driven from chat reads: credentials, the gateway's own
# secrets and the harness state that decides whether capture happens.
DENY_PATHS = (
    "~/.ssh/**",
    "~/.kube/**",
    "~/.config/gh/**",
    "~/.config/sre-harness/**",
    "~/.local/state/sre-harness/**",
    "~/.claude/**",
    "~/.claude.json",
    "~/.aws/**",
    "~/.gnupg/**",
    "~/.docker/config.json",
    "~/.netrc",
    "~/.git-credentials",
    "~/.password-store/**",
    "~/.vault_pass*",
    "**/.env",
    "**/*.env",
    "//proc/**",
)

DENY_BASH = (
    "env",
    "printenv",
    "set",
    "export",
    "declare",
    "gh auth",
    "kubectl config view",
    "kubectl exec",
    "kubectl attach",
    "kubectl cp",
    "kubectl debug",
    "kubectl port-forward",
    "kubectl proxy",
    "kubectl run",
)
# Matched anywhere after the verb: 'kubectl get pods,secrets' names secrets too.
DENY_BASH_ANYWHERE = (
    "kubectl get *secret",
    "kubectl describe *secret",
)
DENY_TOOLS = ("WebFetch", "WebSearch")

# What a site may add. Whole tools that write, and any rule that starts with a
# wildcard, would remove the approval gate rather than widen what is readable.
SITE_TOOL_RULE = re.compile(r"^(?:Read|Grep|Glob)\(.+\)$|^mcp__[A-Za-z0-9_-]+(?:__[A-Za-z0-9_*-]+)?$")
SITE_BASH_RULE = re.compile(r"^Bash\(([A-Za-z0-9_./-][^()*]*?)( \*)?\)$")
SHELL_CHARACTERS = re.compile(r"[;&|<>$`\\'\"\n]")
# A first word that runs other commands: allowing it allows anything.
WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "su",
        "sh",
        "bash",
        "zsh",
        "dash",
        "fish",
        "env",
        "xargs",
        "eval",
        "exec",
        "nohup",
        "timeout",
        "watch",
        "nice",
        "time",
        "command",
        "builtin",
        "find",
        "awk",
        "gawk",
        "perl",
        "ruby",
        "node",
        "npx",
        "uv",
        "uvx",
        "pipx",
        "make",
    }
)
INTERPRETER = re.compile(r"^python[0-9.]*$")
REMOTE_SHELLS = frozenset({"ssh", "mosh"})


def harness_rules(scripts_dir: Path = SCRIPTS) -> list[str]:
    """Allow rules for the harness's own scripts, by absolute path."""
    found = []
    for call in HARNESS_CALLS:
        script, _, rest = call.partition(" ")
        prefix = f"uv run {scripts_dir / script}"
        found.append(f"Bash({prefix} {rest} *)" if rest else f"Bash({prefix} *)")
    return found


def default_allow(scripts_dir: Path = SCRIPTS) -> list[str]:
    """The plugin's read-only allow-list."""
    return [*READ_ONLY_TOOLS, *harness_rules(scripts_dir), *(f"Bash({cmd} *)" for cmd in READ_ONLY_BASH)]


def default_deny() -> list[str]:
    """The plugin's deny-list. A Read rule also blocks edits of the same path; Edit is listed for clarity."""
    found = list(DENY_TOOLS)
    for path in DENY_PATHS:
        found += [f"Read({path})", f"Edit({path})"]
    found += [f"Bash({cmd} *)" for cmd in DENY_BASH]
    found += [f"Bash({cmd}*)" for cmd in DENY_BASH_ANYWHERE]
    return found


def check_site_rule(rule: str) -> tuple[str | None, str | None]:
    """(why the rule is refused, a warning about it) for one of a site's allow rules."""
    if SITE_TOOL_RULE.fullmatch(rule):
        return None, None
    match = SITE_BASH_RULE.fullmatch(rule)
    if not match:
        return (
            "must be Bash(<command prefix> *), Read/Grep/Glob(<path>) or mcp__<server>[__<tool>]; "
            "whole tools, Edit, Write and rules starting with a wildcard are refused"
        ), None
    prefix, wildcard = match.group(1).strip(), bool(match.group(2))
    problem = _bash_prefix_problem(prefix, wildcard)
    if problem:
        return problem, None
    if prefix.split()[0] in REMOTE_SHELLS and wildcard:
        return None, "a remote shell prefix lets any command on that host run without approval"
    return None, None


def _bash_prefix_problem(prefix: str, wildcard: bool) -> str | None:
    """Why a site's Bash allow rule is refused, checked in order; None if none applies."""
    words = prefix.split() or [""]
    checks = (
        (bool(SHELL_CHARACTERS.search(prefix)), "the command prefix may not contain quotes or shell operators"),
        (not words[0], "a Bash rule needs a command prefix"),
        (
            words[0] in WRAPPERS or bool(INTERPRETER.fullmatch(words[0])),
            f"'{words[0]}' runs other commands; allowing it would allow anything",
        ),
        (
            wildcard and len(words) < 2 and words[0] not in REMOTE_SHELLS,
            "a rule ending in a wildcard needs at least two words, so it cannot cover a whole program",
        ),
        (
            audit.is_mutation(prefix),
            "the command prefix looks like a mutation; approve it per call, or list it in Tier 2",
        ),
        (
            any(prefix == denied or prefix.startswith(f"{denied} ") for denied in DENY_BASH),
            "the command prefix is on the plugin's deny-list",
        ),
    )
    return next((message for failed, message in checks if failed), None)


def site_findings(chat: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings) about a site's chat.allow rules."""
    errors, warnings = [], []
    for index, rule in enumerate(chat.get("allow", [])):
        problem, warning = check_site_rule(rule)
        if problem:
            errors.append(f"chat/allow/{index}: {problem}")
        if warning:
            warnings.append(f"chat/allow/{index}: {warning}")
    return errors, warnings


def rules(chat: dict[str, Any], scripts_dir: Path = SCRIPTS) -> dict[str, list[str]]:
    """The merged permission rules. Raises ValueError if a site rule is refused."""
    problems, _warnings = site_findings(chat)
    if problems:
        raise ValueError("; ".join(problems))
    allow = default_allow(scripts_dir)
    allow += [rule for rule in chat.get("allow", []) if rule not in allow]
    return {"allow": allow, "deny": default_deny()}


def settings_json(chat: dict[str, Any], scripts_dir: Path = SCRIPTS) -> str:
    """Inline settings for the agent: the permission rules and nothing else."""
    return json.dumps({"permissions": rules(chat, scripts_dir)}, sort_keys=True)
