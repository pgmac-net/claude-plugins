"""Decide which simple commands in a shell line change a system.

Used by audit.py to find mutations that ran without an approval. The model:

* A command line is split into simple commands at unquoted operators and newlines.
  Comments, heredoc bodies and command substitutions are taken out first.
* Each simple command is judged by its program word, after leading variable
  assignments, shell keywords and wrappers (sudo, timeout, env, ...) are removed.
* Carriers run another command: ssh's remote command, sh/bash/su -c, watch, xargs,
  find -exec, docker exec, kubectl exec. What they carry is judged the same way.
* Any other argument that holds whitespace or shell syntax is parsed again as a
  command line, since a program may run it; plain prose starts with an ordinary
  word and is not a mutation.
* A heredoc body is data, unless the command it feeds reads commands from stdin
  (a shell with no -c, ssh with no remote command). A substitution is always run.

It fails closed: a command line too long to classify is a mutation; one that cannot
be parsed is matched by its whole text; a nested argument that cannot be parsed is
matched at every place a command could start in it.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_CLASSIFY = 20000
MAX_DEPTH = 8

# The whole-text fallback for a command line that cannot be parsed. Deliberately broad:
# a false positive costs a line in a report, a false negative hides an unapproved change.
MUTATING = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bkubectl\b.{0,400}\b(delete|apply|create|patch|replace|edit|scale|cordon|uncordon|drain|taint|label)\b",
        r"\bkubectl\b.{0,400}\b(annotate|set)\b",
        r"\bkubectl\b.{0,400}\brollout\s+(restart|undo|pause|resume)\b",
        r"\bhelm\b.{0,400}\b(install|upgrade|uninstall|rollback|delete)\b",
        r"\bsystemctl\b.{0,200}\b(start|stop|restart|reload|enable|disable|kill|mask|unmask)\b",
        r"\bservice\s+\S{1,100}\s+(start|stop|restart|reload)\b",
        r"\bdocker\b.{0,200}\b(restart|stop|start|kill|rm|rmi|prune|run|exec)\b",
        r"\b(docker\s+compose|docker-compose)\b.{0,200}\b(up|down|restart|stop|start|rm|pull)\b",
        r"\b(crictl|ctr)\b.{0,200}\b(rm|rmp|rmi|stop|stopp|kill)\b",
        r"\bterraform\b.{0,200}\b(apply|destroy|import|taint|untaint)\b|\bterraform\s+state\s+(rm|mv)\b",
        r"\bansible(-playbook)?\b",
        r"\b(reboot|shutdown|poweroff|halt)\b|\binit\s+[06]\b",
        r"\b(rm|rmdir|mv|truncate|shred|dd|mkfs\S{0,20}|fsck\S{0,20}|mount|umount|chmod|chown|ln)\s",
        r"\b(kill|pkill|killall)\b",
        r"\bgit\b.{0,200}\b(push|reset|rebase|merge|revert)\b",
        r"\b(apt|apt-get|dnf|yum|snap|pip|npm)\b.{0,200}\b(install|remove|purge|upgrade|refresh|uninstall)\b",
        r"\b(iptables|nft)\b|\bip\s+(route|link|addr)\s+(add|del|delete|replace|flush|set)\b",
        r"\bqm\s+(start|stop|shutdown|reset|reboot|destroy|set|migrate)\b",
        r"\btee\b|\bsed\s+(-\S{0,8}i|--in-place)",
    )
)
# Where a command can start inside text that is not valid shell: the start, or after an operator or quote.
COMMAND_START = re.compile(r"(?:^|[;&|\n(`'\"])\s*(?:(?:sudo(?:\s+-\S+)*|nohup|timeout\s+\S+|env|exec|command)\s+)*")

# The harness's own record-keeping changes the tracker and the audit branch; that is not a
# system change. Only a segment that is itself one plain call of a harness script is exempt.
HARNESS_SCRIPTS = frozenset(
    {"incident.py", "timeline.py", "audit.py", "autonomy.py", "catalog_sync.py", "catalog_check.py", "validate_site.py"}
)
SCRIPTS_DIR = Path(__file__).resolve().parent
SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")

# Placeholders for what _prepare takes out of a line. A shell command cannot contain NUL, so input cannot forge one.
SUB_MARK = re.compile("\x00S(\\d+)\x00")
HEREDOC_MARK = re.compile("\x00H(\\d+)\x00")
OPERATOR_CHARS = frozenset(";&|<>()")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
LEADING_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "time"})
DATA_KEYWORDS = frozenset({"for", "case", "select", "in", "done", "fi", "esac", "function", "[[", "]]"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish"})
SIMPLE_WORD = re.compile(r"[A-Za-z0-9-]+")
VARIABLE_WORD = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
DECLARATIONS = frozenset({"export", "local", "readonly", "declare", "typeset"})


@dataclass(frozen=True)
class Heredoc:
    body: str
    quoted: bool


@dataclass
class _Prepared:
    text: str
    subs: list[str] = field(default_factory=list)
    heredocs: list[Heredoc] = field(default_factory=list)


@dataclass
class _Segment:
    argv: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)


@dataclass
class Unit:
    """One simple command of a line, and the simple commands within it that change a system."""

    argv: tuple[str, ...]
    hits: list[tuple[str, ...]]
    reads_stdin: bool = False


class _Unparsable(ValueError):
    pass


# --------------------------------------------------------------------------- scanning


def _close(text: str, start: int) -> int:
    """Index of the ')' that closes the '(' at text[start], reading quotes as the shell does."""
    depth, i, single, double = 1, start + 1, False, False
    while i < len(text):
        char = text[i]
        if single:
            single = char != "'"
        elif char == "\\":
            i += 1
        elif char == '"':
            double = not double
        elif char == "'" and not double:
            single = True
        elif char == "`":
            end = text.find("`", i + 1)
            if end < 0:
                raise _Unparsable("unterminated backquote")
            i = end
        elif not double and char == "(":
            depth += 1
        elif not double and char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Unparsable("unbalanced parenthesis")


def _backquote_end(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    raise _Unparsable("unterminated backquote")


def _heredoc_word(text: str, i: int) -> tuple[str, bool, bool, int]:
    """(delimiter, quoted, strip_tabs, next index) for the heredoc operator ending just before text[i]."""
    strip = text.startswith("-", i)
    i += 1 if strip else 0
    while i < len(text) and text[i] in " \t":
        i += 1
    start = i
    while i < len(text) and not text[i].isspace() and text[i] not in OPERATOR_CHARS:
        i += 1
    word = text[start:i]
    delimiter = word.replace("'", "").replace('"', "").replace("\\", "")
    if not delimiter:
        raise _Unparsable("heredoc without a delimiter")
    return delimiter, delimiter != word, strip, i


def _heredoc_bodies(text: str, i: int, pending: list[tuple[str, bool, bool]]) -> tuple[list[Heredoc], int]:
    """Read the bodies of the heredocs opened on a line, starting just after its newline."""
    bodies = []
    for delimiter, quoted, strip in pending:
        lines = []
        while i < len(text):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            line = text[i:end]
            i = end + 1
            if (line.lstrip("\t") if strip else line) == delimiter:
                break
            lines.append(line)
        bodies.append(Heredoc("\n".join(lines), quoted))
    return bodies, i


# One pass over the line, one branch per shell construct.
def _prepare(command: str) -> _Prepared:  # noqa: PLR0912, PLR0915  pylint: disable=too-many-branches,too-many-statements
    """Take comments, substitutions and heredoc bodies out of a line; newlines become ';'."""
    out: list[str] = []
    prepared = _Prepared("")
    pending: list[tuple[str, bool, bool]] = []
    single = double = False
    i = 0
    while i < len(command):
        char = command[i]
        if single:
            out.append(char)
            single = char != "'"
            i += 1
        elif char == "\\":
            if command.startswith("\n", i + 1):
                i += 2  # line continuation
            else:
                out.append(command[i : i + 2])
                i += 2
        elif char == '"':
            double = not double
            out.append(char)
            i += 1
        elif char == "'" and not double:
            single = True
            out.append(char)
            i += 1
        elif command.startswith("$((", i):
            end = _close(command, i + 1)
            # Arithmetic is not a command, but it can hold a substitution.
            inner = _prepare(command[i + 3 : end - 1] if command[end - 1] == ")" else command[i + 2 : end])
            prepared.subs.extend(inner.subs)
            out.append("0")
            i = end + 1
        elif command.startswith("$(", i) or (not double and command.startswith(("<(", ">("), i)):
            end = _close(command, i + 1)
            prepared.subs.append(command[i + 2 : end])
            out.append(f"\x00S{len(prepared.subs) - 1}\x00")
            i = end + 1
        elif char == "`":
            end = _backquote_end(command, i)
            prepared.subs.append(command[i + 1 : end].replace("\\`", "`"))
            out.append(f"\x00S{len(prepared.subs) - 1}\x00")
            i = end + 1
        elif not double and command.startswith("<<", i) and not command.startswith("<<<", i):
            delimiter, quoted, strip, i = _heredoc_word(command, i + 2)
            pending.append((delimiter, quoted, strip))
            out.append(f" << \x00H{len(prepared.heredocs) + len(pending) - 1}\x00 ")
        elif not double and char == "#" and (not out or out[-1][-1:] in ("", " ", "\t", ";", "&", "|", "(", ")")):
            end = command.find("\n", i)
            i = len(command) if end < 0 else end  # a comment runs to the end of the line
        elif not double and char == "\n":
            out.append(" ; ")
            i += 1
            if pending:
                bodies, i = _heredoc_bodies(command, i, pending)
                prepared.heredocs.extend(bodies)
                pending = []
        else:
            out.append(char)
            i += 1
    prepared.heredocs.extend(Heredoc("", quoted) for _delimiter, quoted, _strip in pending)
    prepared.text = "".join(out)
    return prepared


def _split(text: str) -> list[_Segment]:
    """Simple commands, split at unquoted operators; redirection targets kept apart from the words."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # bash starts a comment only at the start of a word, and _prepare has already removed them.
    lexer.commenters = ""
    segments = [_Segment()]
    target = False
    for token in lexer:
        if token and set(token) <= OPERATOR_CHARS:
            target = "<" in token or ">" in token
            if target and segments[-1].argv and segments[-1].argv[-1].isdigit():
                segments[-1].redirects.append(segments[-1].argv.pop())  # the '2' of '2>&1' names a stream
            # '>', '2>&', '&>' redirect within one command; anything else ('&&', '|', ';', '(') separates two.
            if not target or set(token) & set(";|()"):
                segments.append(_Segment())
        elif target:
            segments[-1].redirects.append(token)
            target = False
        else:
            segments[-1].argv.append(token)
    return [segment for segment in segments if segment.argv or segment.redirects]


def heredoc_substitutions(body: str) -> list[str]:
    """The commands substituted into an unquoted heredoc body, where quotes are plain text."""
    found, i = [], 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        try:
            if body.startswith("$((", i):
                i += 3
                continue
            if body.startswith("$(", i):
                end = _close(body, i + 1)
                found.append(body[i + 2 : end])
                i = end + 1
                continue
            if body[i] == "`":
                end = _backquote_end(body, i)
                found.append(body[i + 1 : end])
                i = end + 1
                continue
        except _Unparsable:
            found.append(body[i:])  # fail closed: the rest is judged as a command
            break
        i += 1
    return found


# --------------------------------------------------------------------------- wrappers and carriers


def _after_options(words: list[str], takes_value: frozenset[str], skip: int = 0) -> list[str]:
    """The words after a program's options and its first `skip` operands."""
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if word.startswith("-") and len(word) > 1:
            name = word.split("=", 1)[0]
            if name in takes_value and "=" not in word and (len(word) == 2 or word.startswith("--")):
                i += 1
            i += 1
            continue
        break
    return words[i + skip :]


def _shell_payload(words: list[str]) -> tuple[str, object] | None:
    """What a shell runs: ('shell', script) for -c, ('stdin',) when it reads commands from stdin."""
    skip = False
    for index, word in enumerate(words[1:], start=1):
        if skip:
            skip = False  # the value of -o/+o/-O, not a script
            continue
        if word in ("-o", "+o", "-O", "+O"):
            skip = True
        elif word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            rest = [w for w in words[index + 1 :] if not w.startswith("-")]
            return ("shell", rest[0]) if rest else None
        elif not word.startswith(("-", "+")):
            return None  # a script file: what it does is not in the command line
    return ("stdin", None)


def _ssh_payload(words: list[str]) -> tuple[str, object]:
    rest = _after_options(words, frozenset("-B -b -c -D -E -e -F -I -i -J -L -l -m -O -o -P -p -Q -R -S -W -w".split()))
    remote = rest[1:]
    if remote[:1] == ["--"]:
        remote = remote[1:]
    return ("shell", " ".join(remote)) if remote else ("stdin", None)


def _sudo_payload(words: list[str]) -> tuple[str, object]:
    rest = _after_options(
        words, frozenset("-u -g -C -D -h -p -r -t -U -T -R --user --group --prompt --chdir --host".split())
    )
    if not rest and {"-s", "-i", "--shell", "--login"} & set(words):
        return ("stdin", None)  # a root shell reading commands, from a heredoc or pipe
    return ("argv", rest)


def _su_payload(words: list[str]) -> tuple[str, object]:
    for index, word in enumerate(words):
        if word in ("-c", "--command") and index + 1 < len(words):
            return ("shell", words[index + 1])
        if word.startswith("--command="):
            return ("shell", word.split("=", 1)[1])
    return ("stdin", None)


def _watch_payload(words: list[str]) -> tuple[str, object]:
    rest = _after_options(words, frozenset("-n --interval -d --differences -q --equexit".split()))
    return ("argv", rest) if "-x" in words or "--exec" in words else ("shell", " ".join(rest))


def _find_payload(words: list[str]) -> tuple[str, object] | None:
    for index, word in enumerate(words):
        if word in ("-exec", "-execdir", "-ok", "-okdir"):
            rest = words[index + 1 :]
            end = next((i for i, w in enumerate(rest) if w in (";", "+")), len(rest))
            return ("argv", rest[:end])
    return None


def _env_payload(words: list[str]) -> tuple[str, object]:
    for index, word in enumerate(words):
        if word in ("-S", "--split-string") and index + 1 < len(words):
            return ("shell", " ".join(words[index + 1 :]))
    return ("argv", _after_options(words, frozenset("-u --unset -C --chdir".split())))


def _argv(takes_value: str = "", skip: int = 0) -> Callable[[list[str]], tuple[str, object]]:
    options = frozenset(takes_value.split())
    return lambda words: ("argv", _after_options(words, options, skip))


WRAPPERS: dict[str, Callable[[list[str]], tuple[str, object] | None]] = {
    "sudo": _sudo_payload,
    "eval": lambda words: ("shell", " ".join(words[1:])),
    "doas": _argv("-u -C"),
    "env": _env_payload,
    "timeout": _argv("-s -k --signal --kill-after", skip=1),
    "nice": _argv("-n --adjustment"),
    "ionice": _argv("-c -n -p --class --classdata"),
    "nohup": _argv(),
    "setsid": _argv(),
    "stdbuf": _argv("-i -o -e --input --output --error"),
    "time": _argv("-f -o --format --output"),
    "command": lambda words: None if {"-v", "-V"} & set(words) else ("argv", _after_options(words, frozenset())),
    "builtin": _argv(),
    "exec": _argv("-a"),
    "unbuffer": _argv(),
    "chroot": _argv("--userspec --groups", skip=1),
    "flock": _argv("-w -E --timeout --conflict-exit-code", skip=1),
    "nsenter": _argv("-t --target -S --setuid -G --setgid -w --wd -r --root"),
    "xargs": _argv("-I -i -n -P -L -l -d -E -e -s -a --arg-file --max-args --max-procs --delimiter --replace"),
    "watch": _watch_payload,
    "ssh": _ssh_payload,
    "su": _su_payload,
    "find": _find_payload,
}


# --------------------------------------------------------------------------- programs that change a system


def _has_verb(words: list[str], verbs: frozenset[str]) -> bool:
    """A word that is one of the verbs, or a plain hyphenated word with one of them in it (try-restart)."""
    return any(
        word in verbs or (SIMPLE_WORD.fullmatch(word) is not None and any(part in verbs for part in word.split("-")))
        for word in words
    )


def _verbs(verbs: str) -> Callable[[list[str]], bool]:
    names = frozenset(verbs.split())
    return lambda words: _has_verb(words[1:], names)


def _has_operand(words: list[str]) -> bool:
    return len(words) > 1


def _mount(words: list[str]) -> bool:
    """A mount with an operand mounts something; with options alone it lists what is mounted."""
    if {"-a", "--all", "--source", "--target"} & {word.split("=", 1)[0] for word in words}:
        return True
    return bool(_after_options(words, frozenset("-t -o -O -L -U -T --types --options --test-opts".split())))


def _sed(words: list[str]) -> bool:
    return any(
        word.startswith("--in-place") or (re.fullmatch(r"-[A-Za-z]*i\S*", word) is not None and word != "-i-")
        for word in words[1:]
    )


def _terraform(words: list[str]) -> bool:
    if _has_verb(words[1:], frozenset("apply destroy import taint untaint".split())):
        return True
    return "state" in words and _has_verb(words[words.index("state") + 1 :], frozenset({"rm", "mv"}))


def _packages(words: list[str]) -> bool:
    tools = {"apt", "apt-get", "dnf", "yum", "snap", "pip", "npm"}
    first = next((i for i, word in enumerate(words) if word in tools), None)
    verbs = frozenset("install remove purge upgrade refresh uninstall".split())
    return first is not None and _has_verb(words[first + 1 :], verbs)


def _ip(words: list[str]) -> bool:
    rest = [word for word in words[1:] if not word.startswith("-")]
    verbs = {"add", "del", "delete", "replace", "flush", "set"}
    return len(rest) >= 2 and rest[0] in ("route", "link", "addr") and rest[1] in verbs


RULES: dict[str, Callable[[list[str]], bool]] = {
    "helm": _verbs("install upgrade uninstall rollback delete"),
    "systemctl": _verbs(
        "start stop restart reload enable disable kill mask unmask reboot poweroff halt shutdown isolate"
    ),
    "service": lambda words: len(words) > 2 and words[2] in ("start", "stop", "restart", "reload"),
    "docker-compose": _verbs("up down restart stop start rm pull"),
    "crictl": _verbs("rm rmp rmi stop stopp kill"),
    "ctr": _verbs("rm rmp rmi stop stopp kill"),
    "terraform": _terraform,
    "init": lambda words: len(words) > 1 and words[1] in ("0", "6"),
    "dd": lambda words: any(word.startswith("of=") for word in words[1:]),
    "mount": _mount,
    "git": _verbs("push reset rebase merge revert"),
    "ip": _ip,
    "qm": lambda words: len(words) > 1 and words[1] in "start stop shutdown reset reboot destroy set migrate".split(),
    "sed": _sed,
    "tee": lambda _words: True,
    "nft": lambda _words: True,
    **dict.fromkeys(("reboot", "shutdown", "poweroff", "halt", "kill", "pkill", "killall"), lambda _words: True),
    **dict.fromkeys(("rm", "rmdir", "mv", "truncate", "shred", "umount", "chmod", "chown", "ln"), _has_operand),
}
KUBECTL_VERBS = frozenset(
    "delete apply create patch replace edit scale cordon uncordon drain taint label annotate set".split()
)
DOCKER_VERBS = frozenset("restart stop start kill rm rmi prune run".split())
DOCKER_OBJECTS = frozenset("container image volume network system builder service stack node plugin secret".split())


def _program_rule(program: str, words: list[str]) -> bool:
    if program in RULES:
        return RULES[program](words)
    if program.startswith(("ansible", "iptables", "mkfs", "fsck")):
        return program.startswith(("ansible", "iptables")) or _has_operand(words)
    return _packages(words)


# --------------------------------------------------------------------------- classification


def is_harness_call(tokens: list[str]) -> bool:
    """True only for a plain call of one of this plugin's own scripts.

    Accepted forms: '<script> …', 'uv run <script> …', 'python3 <script> …'. No
    option may sit between the launcher and the script: an option could name
    other code to run. The script must be given as an absolute path to a file
    in this plugin's scripts directory, not merely a path that ends the same way.
    """
    words = list(tokens)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    elif words and re.fullmatch(r"python3?(\.\d+)?", words[0]):
        words = words[1:]
    if not words or words[0].startswith("-"):
        return False
    try:
        given = Path(words[0]).expanduser()
        # A relative path would be resolved against wherever verify happens to run, not
        # where the command ran. Only an absolute path can be trusted to name our script.
        if not given.is_absolute():
            return False
        script = given.resolve()
    except (OSError, RuntimeError):
        return False
    return script.parent == SCRIPTS_DIR and script.name in HARNESS_SCRIPTS and script.is_file()


@dataclass(frozen=True)
class _Context:
    """Where a simple command sits: how deep, the line's variables, and whether a program really runs it.

    A speculative command comes from an argument parsed again because its program might run it;
    an expansion in its program position is ordinary text, not a command whose name is unknown.
    """

    depth: int = 0
    variables: dict[str, str] = field(default_factory=dict)
    speculative: bool = False

    def inner(self, speculative: bool | None = None) -> _Context:
        return _Context(self.depth + 1, self.variables, self.speculative if speculative is None else speculative)

    def fresh(self, speculative: bool | None = None) -> _Context:
        """For a new shell (ssh's remote side, sh -c, a heredoc fed to a shell): no variables carried over."""
        return _Context(self.depth + 1, {}, self.speculative if speculative is None else speculative)


def _payload_hits(payload: tuple[str, object], ctx: _Context) -> tuple[list[tuple[str, ...]], bool]:
    kind, value = payload
    if kind == "stdin":
        return [], True
    if kind == "shell":
        return [hit for unit in nested_units(str(value), ctx.fresh()) for hit in unit.hits], False
    return classify(list(value), ctx.inner())  # type: ignore[arg-type]


def _kubectl(words: list[str], ctx: _Context) -> list[tuple[str, ...]]:
    head, tail = (words[: words.index("--")], words[words.index("--") + 1 :]) if "--" in words else (words, [])
    hits: list[tuple[str, ...]] = []
    rollout = "rollout" in head and _has_verb(head, frozenset({"restart", "undo", "pause", "resume"}))
    if _has_verb(head[1:], KUBECTL_VERBS) or rollout:
        hits.append(tuple(words))
    if "exec" in head and tail:
        hits.extend(classify(tail, ctx.inner())[0])
    return hits


def _docker(words: list[str], ctx: _Context) -> list[tuple[str, ...]]:
    rest = _after_options(words, frozenset("--context -H --host --config -l --log-level".split()))
    if not rest:
        return []
    sub, args = rest[0], rest[1:]
    if sub in DOCKER_OBJECTS and args:
        sub, args = args[0], args[1:]
    if sub == "compose":
        return [tuple(words)] if _has_verb(args, frozenset("up down restart stop start rm pull".split())) else []
    if sub == "exec":
        carried = _after_options(["exec", *args], frozenset("-e --env -u --user -w --workdir --env-file".split()))
        return classify(carried[1:], ctx.inner())[0] if len(carried) > 1 else []
    return [tuple(words)] if sub in DOCKER_VERBS else []


def _expand(words: list[str], variables: dict[str, str]) -> list[str]:
    """Words that are a variable set earlier in the line ('C1=...; $C1') replaced by its value.

    In program position the value is split into words, as the shell splits an unquoted
    expansion; elsewhere it stays one word.
    """
    expanded = []
    for index, word in enumerate(words):
        match = VARIABLE_WORD.fullmatch(word)
        if match is None or match.group(1) not in variables:
            expanded.append(word)
            continue
        value = variables[match.group(1)]
        if index:
            expanded.append(value)
            continue
        try:
            expanded.extend(shlex.split(value))
        except ValueError:
            expanded.append(value)
    return expanded


def classify(
    argv: list[str], ctx: _Context | None = None, substituted: bool = False
) -> tuple[list[tuple[str, ...]], bool]:
    """(the simple commands in this one that change a system, whether it reads commands from stdin)."""
    ctx = ctx or _Context()
    words = list(argv)
    while words and (ASSIGNMENT.match(words[0]) or words[0] in LEADING_KEYWORDS):
        words.pop(0)  # an assignment's value is data, unless it is run later as a command
    words = _expand(words, ctx.variables)
    if not words or words[0] in DATA_KEYWORDS or words[0] in DECLARATIONS:
        return [], False  # loop and case headers, and declarations, hold data
    if ctx.depth > MAX_DEPTH:
        return [tuple(words)], False
    if not substituted and is_harness_call(words):
        return [], False
    return _judge(words, ctx)


def _judge(words: list[str], ctx: _Context) -> tuple[list[tuple[str, ...]], bool]:
    """classify() for a simple command reduced to its program and arguments."""
    program = words[0].rsplit("/", 1)[-1]
    if "$" in program or "\x00" in program:
        # The program is an expansion whose value is not in the line: fail closed, where a program really runs.
        return ([] if ctx.speculative else [tuple(words)]), False
    if program in ("microk8s", "busybox") and len(words) > 1:
        words, program = words[1:], words[1]
    payload: tuple[str, object] | None = None
    if program in WRAPPERS:
        payload = WRAPPERS[program](words)
    elif program in SHELLS:
        payload = _shell_payload(words)
    elif program in ("kubectl", "docker"):
        return (_kubectl if program == "kubectl" else _docker)(words, ctx), False
    else:
        return _plain(program, words, ctx), False
    return _payload_hits(payload, ctx) if payload else ([], False)


def _plain(program: str, words: list[str], ctx: _Context) -> list[tuple[str, ...]]:
    """A program that is not a carrier: its own rule, and any argument it could run as a command."""
    hits = [tuple(words)] if _program_rule(program, words) else []
    for word in words[1:]:
        # A program may run an argument as a command; prose begins with an ordinary word and is not one.
        if any(char.isspace() for char in word) or SUBSTITUTION.search(word):
            hits.extend(hit for unit in nested_units(word, ctx.fresh(speculative=True)) for hit in unit.hits)
    return hits


def _text_fallback(text: str, anchored: bool) -> bool:
    if not anchored:
        return any(pattern.search(text) for pattern in MUTATING)
    starts = {match.end() for match in COMMAND_START.finditer(text)}
    return any(pattern.match(text, start) for start in starts for pattern in MUTATING)


def units(command: str, ctx: _Context | None = None) -> list[Unit] | None:
    """The simple commands of a line, in the order they run, each with its mutations; None if unparsable."""
    ctx = ctx or _Context()
    try:
        prepared = _prepare(command)
        segments = _split(prepared.text)
    except (_Unparsable, ValueError):
        return None
    result: list[Unit] = []
    judged: set[int] = set()
    line = _Context(ctx.depth, dict(ctx.variables), ctx.speculative)
    for segment in segments:
        _remember_assignments(segment.argv, line.variables)
        marks = [int(n) for token in segment.argv + segment.redirects for n in SUB_MARK.findall(token)]
        for number in marks:
            # A substitution runs before the command it is part of.
            if number not in judged and number < len(prepared.subs):
                judged.add(number)
                result.extend(nested_units(prepared.subs[number], line.inner()))  # a subshell keeps the variables
        hits, reads_stdin = classify(segment.argv, line, substituted=bool(marks))
        result.append(Unit(tuple(segment.argv), hits, reads_stdin))
        for token in segment.redirects:
            for number in (int(n) for n in HEREDOC_MARK.findall(token)):
                if number >= len(prepared.heredocs):
                    continue
                body = prepared.heredocs[number]
                if reads_stdin:
                    result.extend(nested_units(body.body, line.fresh()))
                elif not body.quoted:
                    for text in heredoc_substitutions(body.body):
                        result.extend(nested_units(text, line.fresh()))
    for number, text in enumerate(prepared.subs):
        if number not in judged:
            result.extend(nested_units(text, line.inner()))
    return result


def _remember_assignments(argv: list[str], variables: dict[str, str]) -> None:
    """Record 'NAME=value' words that set a variable for the rest of the line (alone, or after export and the like)."""
    words = argv[1:] if argv[:1] and argv[0] in DECLARATIONS else argv
    names = [word for word in words if ASSIGNMENT.match(word)]
    if words and len(names) == len([word for word in words if not word.startswith("-")]):
        for word in names:
            name, _sep, value = word.partition("=")
            variables[name.rstrip("+").split("[", 1)[0]] = value


def nested_units(text: str, ctx: _Context) -> list[Unit]:
    """units() for text found inside a command; text that does not parse is judged where a command could start."""
    if ctx.depth > MAX_DEPTH:
        return [Unit((text,), [(text,)])]
    found = units(text, ctx)
    if found is None:
        return [Unit((text,), [(text,)] if _text_fallback(text, anchored=True) else [])]
    return found


def is_mutation(command: str) -> bool:
    """Whether a shell command looks like it changes a system.

    Fails closed: a command too long to classify is a mutation, and one that
    cannot be parsed is judged by its whole text.
    """
    if not command.strip():
        return False
    if len(command) > MAX_CLASSIFY:
        return True
    found = units(command)
    if found is None:
        return _text_fallback(command, anchored=False)
    return any(unit.hits for unit in found)


def approval_call(argv: tuple[str, ...]) -> tuple[bool, str | None]:
    """Whether a simple command records an approval with audit.py, and the command it names if written literally."""
    words = list(argv)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    elif words and re.fullmatch(r"python3?(\.\d+)?", words[0]):
        words = words[1:]
    if not words or words[0].rsplit("/", 1)[-1] != "audit.py" or "record" not in words:
        return False, None
    if not any(
        (word == "--kind" and index + 1 < len(words) and words[index + 1] == "approval") or word == "--kind=approval"
        for index, word in enumerate(words)
    ):
        return False, None
    literal = None
    for index, word in enumerate(words):
        if word == "--command" and index + 1 < len(words):
            literal = words[index + 1]
        elif word.startswith("--command="):
            literal = word.split("=", 1)[1]
    if literal is not None and ("$" in literal or "\x00" in literal):
        literal = None  # the value was expanded by the shell; only the record holds it
    return True, literal
