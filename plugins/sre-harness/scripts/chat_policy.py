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
    "incident.py promote",
    "incident.py dismiss",
    "timeline.py",
    "audit.py show",
    "audit.py verify",
    "autonomy.py match",
    "autonomy.py run",
    "assist.py",
    "validate_site.py",
    "catalog_check.py",
    "kube_read.py",
    "slo.py status",
    "slo.py check",
    # Close-out (homelabia#273): the log goes to its own branch with a pull request a person merges, and gc
    # deletes only local state it has proved is preserved. The options that weaken either are denied below.
    "audit.py commit",
    "incident.py gc",
)

# Options of allow-listed harness calls that no chat session may use: skipping the
# check against the timeline's pinned hashes, and clearing state before a person
# has closed the tracking issue.
HARNESS_DENIED_OPTIONS = (
    ("audit.py commit", "--offline"),
    ("incident.py gc", "--ignore-issue-state"),
)

# Read-only commands beyond Claude Code's built-in set. Only commands whose
# every option is read-only are listed: a prefix rule cannot exclude options.
# git and journalctl are left out because some of their options read any file
# (git diff --no-index, journalctl --file) or write one (git log --output,
# journalctl --vacuum-*); Claude Code still runs plain read-only git itself.
# kubectl is left out because its options can send the credential elsewhere
# (--server, or -s bundled with other short flags) or act as another identity;
# kube_read.py, above, checks each argument and is the read path. Anything
# else from these becomes an approval request.
READ_ONLY_BASH = (
    "gh issue view",
    "gh issue list",
    "gh pr view",
    "gh pr list",
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
# A second layer for kubectl run directly, which needs an approval anyway: denied
# outright, wherever they appear. 'kubectl get pods,secrets' names secrets too.
# The connection options would read or use another credential (--kubeconfig,
# --token, --user, --cluster, client certificates), send the configured one to
# another host (--server, -s, with the TLS options that make that work) or act
# as another identity (--as and its variants). Glob patterns cannot see
# bundled short flags; kube_read.py is the control that can.
DENY_BASH_ANYWHERE = (
    "kubectl get *secret",
    "kubectl describe *secret",
    "kubectl *--kubeconfig",
    "kubectl *--token",
    "kubectl *--client-key",
    "kubectl *--client-certificate",
    "kubectl *--server",
    "kubectl * -s",
    "kubectl *--as",
    "kubectl *--user",
    "kubectl *--cluster",
    "kubectl *--username",
    "kubectl *--password",
    "kubectl *--certificate-authority",
    "kubectl *--insecure-skip-tls-verify",
    "kubectl *--tls-server-name",
)
DENY_TOOLS = ("WebFetch", "WebSearch")

# What a site may add. Whole tools that write, and any rule that starts with a
# wildcard, would remove the approval gate rather than widen what is readable.
# An MCP rule names one tool: a whole server, or a wildcard, would also allow
# any tool the server adds later, including one that writes.
SITE_TOOL_RULE = re.compile(r"^(?:Read|Grep|Glob)\(.+\)$|^mcp__[A-Za-z0-9_-]+?__[A-Za-z0-9_-]+$")
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
# Programs whose options a prefix rule cannot constrain: options that redirect a
# credential, act as another identity, read or write any file, transfer data or
# run other programs. Matched by basename, anywhere in the prefix. This is a
# guard rail, not the control: every accepted Bash rule also carries a warning,
# because no list can know every program a site might name.
_CLUSTER = "its options can send the cluster credential to another server; use kube_read.py"
_FILES = "its options can read or write any file"
_TRANSFER = "it transfers data or runs other programs"
NEEDS_ARGUMENT_CHECKS = {
    **dict.fromkeys(("kubectl", "kubecolor", "oc", "k3s", "k0s", "microk8s", "rke2", "kubeadm"), _CLUSTER),
    **dict.fromkeys(("helm", "flux", "argocd", "kustomize", "terraform", "ansible", "kubectx", "kubens"), _CLUSTER),
    **dict.fromkeys(("git", "journalctl", "sed", "tar", "zip", "unzip", "cp", "dd", "tee", "install"), _FILES),
    **dict.fromkeys(("curl", "wget", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "socat", "ftp"), _TRANSFER),
    **dict.fromkeys(("docker", "podman", "nerdctl", "crictl", "ctr", "systemd-run", "at", "crontab"), _TRANSFER),
}
INTERPRETER = re.compile(r"^python[0-9.]*$")
REMOTE_SHELLS = frozenset({"ssh", "mosh"})


def harness_rules(scripts_dir: Path = SCRIPTS) -> list[str]:
    """Allow rules for the harness's own scripts, by absolute path."""
    return harness_rules_for(HARNESS_CALLS, scripts_dir)


def default_allow(scripts_dir: Path = SCRIPTS) -> list[str]:
    """The plugin's read-only allow-list."""
    return [*READ_ONLY_TOOLS, *harness_rules(scripts_dir), *(f"Bash({cmd} *)" for cmd in READ_ONLY_BASH)]


def default_deny(scripts_dir: Path = SCRIPTS) -> list[str]:
    """The plugin's deny-list. A Read rule also blocks edits of the same path; Edit is listed for clarity."""
    found = list(DENY_TOOLS)
    for call, option in HARNESS_DENIED_OPTIONS:
        script, _, rest = call.partition(" ")
        found.append(f"Bash(uv run {scripts_dir / script} {rest} *{option}*)")
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
            "must be Bash(<command prefix> *), Read/Grep/Glob(<path>) or mcp__<server>__<tool>; "
            "whole tools, whole MCP servers, Edit, Write and rules starting with a wildcard are refused"
        ), None
    prefix, wildcard = match.group(1).strip(), bool(match.group(2))
    problem = _bash_prefix_problem(prefix, wildcard)
    if problem:
        return problem, None
    if Path(prefix.split()[0]).name in REMOTE_SHELLS and wildcard:
        return None, "a remote shell prefix lets any command on that host run without approval"
    return None, (
        f"'{prefix}' runs without approval with any arguments: check that none of its options writes a file, "
        "changes state, reads a credential or contacts another host"
    )


def _bash_prefix_problem(prefix: str, wildcard: bool) -> str | None:
    """Why a site's Bash allow rule is refused, checked in order; None if none applies."""
    words = [Path(word).name for word in prefix.split()] or [""]
    risky = next((word for word in words if word in NEEDS_ARGUMENT_CHECKS), None)
    checks = (
        (bool(SHELL_CHARACTERS.search(prefix)), "the command prefix may not contain quotes or shell operators"),
        (not words[0], "a Bash rule needs a command prefix"),
        (
            words[0] in WRAPPERS or bool(INTERPRETER.fullmatch(words[0])),
            f"'{words[0]}' runs other commands; allowing it would allow anything",
        ),
        (
            risky is not None,
            f"a rule naming '{risky}' cannot constrain its options: {NEEDS_ARGUMENT_CHECKS.get(risky or '', '')}",
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
    return {"allow": allow, "deny": default_deny(scripts_dir)}


def triage_rules(chat: dict[str, Any], scripts_dir: Path = SCRIPTS) -> dict[str, list[str]]:
    """The rules for an unattended triage: read, open a private record, write its timeline. Nothing else.

    The harness calls that change what the incident is, or the system, are
    withheld and denied outright: closing, promoting, dismissing, attaching
    (which would write to a record a person owns) and running a pre-approved
    action. 'open' is allowed only as 'open --auto', so the record goes to the
    private runner tracker; any other open reaches the gateway, which refuses
    every call a triage-only session asks about.
    """
    normal = rules(chat, scripts_dir)
    withheld = {f"Bash(uv run {scripts_dir / call.split()[0]} {call.split()[1]} *)" for call in TRIAGE_WITHHELD}
    auto_open = f"Bash(uv run {scripts_dir / 'incident.py'} open --auto *)"
    allow = [rule for rule in normal["allow"] if rule not in withheld] + [auto_open]
    open_rule = f"Bash(uv run {scripts_dir / 'incident.py'} open *)"
    deny = normal["deny"] + sorted(rule for rule in withheld if rule != open_rule)
    return {"allow": allow, "deny": deny}


# Harness calls a triage-only session may not make; see triage_rules.
TRIAGE_WITHHELD = (
    "incident.py open",
    "incident.py attach",
    "incident.py close",
    "incident.py promote",
    "incident.py dismiss",
    "autonomy.py run",
    "audit.py commit",
    "incident.py gc",
)


# Harness calls a question may not make: everything that writes a record or a system. See question_rules.
QUESTION_WITHHELD = (
    "incident.py open",
    "incident.py attach",
    "incident.py set",
    "incident.py close",
    "incident.py promote",
    "incident.py dismiss",
    "timeline.py",
    "autonomy.py run",
    "audit.py commit",
    "incident.py gc",
)


def question_rules(chat: dict[str, Any], scripts_dir: Path = SCRIPTS) -> dict[str, list[str]]:
    """The rules for a question: the chat allow-list without any call that writes a record or a system.

    Derived from rules(), so a question reads whatever a chat session reads,
    site additions included. The withheld calls are denied outright; any other
    call outside the allow-list reaches the gateway, which refuses every call a
    question asks about.
    """
    normal = rules(chat, scripts_dir)
    withheld = set(harness_rules_for(QUESTION_WITHHELD, scripts_dir))
    allow = [rule for rule in normal["allow"] if rule not in withheld]
    return {"allow": allow, "deny": normal["deny"] + sorted(withheld)}


def harness_rules_for(calls: tuple[str, ...], scripts_dir: Path = SCRIPTS) -> list[str]:
    """The allow-rule form of some harness calls, as harness_rules writes them."""
    found = []
    for call in calls:
        script, _, rest = call.partition(" ")
        prefix = f"uv run {scripts_dir / script}"
        found.append(f"Bash({prefix} {rest} *)" if rest else f"Bash({prefix} *)")
    return found


def settings_json(
    chat: dict[str, Any], scripts_dir: Path = SCRIPTS, triage: bool = False, question: bool = False
) -> str:
    """Inline settings for the agent: the permission rules and nothing else."""
    if question:
        chosen = question_rules(chat, scripts_dir)
    else:
        chosen = triage_rules(chat, scripts_dir) if triage else rules(chat, scripts_dir)
    return json.dumps({"permissions": chosen}, sort_keys=True)
