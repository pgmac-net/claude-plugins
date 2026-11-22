#!/usr/bin/env python3
"""A command-line front end for the harness scripts, with shell completion.

For a person at a terminal. Agents and skills keep running the scripts by full
path (`uv run <plugin root>/scripts/<script>.py`), because the chat policy and
the audit log match that form.

Commands:
  <group> [<command>] [options]   run the group's script, with every word after the group passed through unchanged
  completion bash|zsh|fish        print the completion script for a shell
  completion --regenerate         rebuild srectl_spec.json from the scripts' own argument parsers
  completion --check              exit 1 when srectl_spec.json is stale

This file uses only the standard library, so it starts fast and completion needs no uv. Install by linking it
onto PATH (the real path is resolved, so the scripts are found):  ln -s <plugin root>/scripts/srectl.py ~/bin/srectl
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

SCRIPTS = Path(__file__).resolve().parent
SPEC_PATH = SCRIPTS / "srectl_spec.json"
SPEC_VERSION = 1
PROG = "srectl"
SHELLS = ("bash", "zsh", "fish")

# Marker line `__complete` prints when the shell should complete file names itself.
FILES = ":files"
PATH_FLAGS = frozenset({"--site", "--out"})
INCIDENT_FLAGS = frozenset({"--incident"})
SERVICE_FLAGS = frozenset({"--service", "--services"})
SLO_FLAGS = frozenset({"--slo"})
SITE_ENV = "SRE_HARNESS_SITE"
SERVICE_PREFIX = "component:"
# Commands whose --service takes only the prefixed form; everywhere else a bare name is accepted too.
PREFIX_REQUIRED = frozenset({("autonomy", "--service")})

# (words that name the entry, script, section). A group of several entries has a verb after the group name.
REGISTRY: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("incident",), "incident.py", "Run an incident"),
    (("timeline",), "timeline.py", "Run an incident"),
    (("audit",), "audit.py", "Run an incident"),
    (("autonomy",), "autonomy.py", "Run an incident"),
    (("assist",), "assist.py", "Run an incident"),
    (("alerts",), "alerts.py", "Run an incident"),
    (("slo",), "slo.py", "Reliability"),
    (("budget",), "budget.py", "Reliability"),
    (("stpa",), "stpa.py", "Reliability"),
    (("report",), "report.py", "Reliability"),
    (("trends",), "trends.py", "Reliability"),
    (("catalog", "check"), "catalog_check.py", "Site"),
    (("catalog", "sync"), "catalog_sync.py", "Site"),
    (("site", "check"), "validate_site.py", "Site"),
)
# Groups with no script of their own: the line shown beside the group name.
GROUP_HELP = {
    "catalog": "Validate the service catalog, or regenerate its generated layer",
    "site": "Validate the site repository",
}


def groups() -> list[str]:
    """Group names in registry order."""
    seen: list[str] = []
    for words, _script, _section in REGISTRY:
        if words[0] not in seen:
            seen.append(words[0])
    return seen


def verbs(group: str) -> list[str]:
    """The verbs of a group made of several scripts; empty for a group that is one script."""
    return [words[1] for words, _s, _x in REGISTRY if words[0] == group and len(words) > 1]


def entry_for(words: list[str]) -> tuple[tuple[str, ...], str] | None:
    """The registry entry the leading words name, with its script, or None."""
    for key, script, _section in REGISTRY:
        if tuple(words[: len(key)]) == key:
            return key, script
    return None


def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


# --- the spec: what completion and group help know about the scripts -----------------------------------


def describe_parser(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Options, subcommands and help text of one argparse parser, as plain data.

    argparse has no public way to walk a parser, so this reads its private action list on purpose.
    """
    # pylint: disable=protected-access
    options: list[dict[str, Any]] = []
    commands: dict[str, Any] = {}
    required = False
    for action in parser._actions:  # noqa: SLF001  pylint: disable=protected-access
        if isinstance(action, argparse._HelpAction) or action.help == argparse.SUPPRESS:  # noqa: SLF001
            continue
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            required = bool(action.required)
            helps = {choice.dest: one_line(choice.help) for choice in action._choices_actions}  # noqa: SLF001
            for name, sub in action.choices.items():
                described = describe_parser(sub)
                commands[name] = {"help": helps.get(name, ""), "options": described["options"]}
        elif action.option_strings:
            options.append(
                {
                    "flags": list(action.option_strings),
                    "value": action.nargs != 0,
                    "choices": [str(choice) for choice in action.choices] if action.choices else None,
                    "help": one_line(action.help),
                }
            )
    return {"help": one_line(parser.description), "options": options, "required": required, "commands": commands}


class _Captured(Exception):
    """Raised from a patched parse_args to hand the parser back."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("parser captured")
        self.parser = parser


def capture_parser(script: Path) -> argparse.ArgumentParser:
    """Load a script without running it and return its top-level argument parser."""
    sys.path.insert(0, str(SCRIPTS))
    name = f"_srectl_capture_{script.stem}"
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    try:
        spec.loader.exec_module(module)
        builder = getattr(module, "build_parser", None)
        if builder is not None:
            return builder()
        return _capture_from_main(module, script)
    finally:
        sys.modules.pop(name, None)


def _capture_from_main(module: Any, script: Path) -> argparse.ArgumentParser:
    """For a script that builds its parser inside main(): stop parse_args and keep the parser."""

    original = argparse.ArgumentParser.parse_args

    def grab(self: argparse.ArgumentParser, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = grab  # type: ignore[method-assign,assignment]
    try:
        module.main([])
    except _Captured as caught:
        return caught.parser
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    raise RuntimeError(f"{script.name}: no argument parser found")


def build_spec() -> dict[str, Any]:
    """Describe every registered script. Needs the scripts' own dependencies importable."""
    entries: dict[str, Any] = {}
    for words, script, _section in REGISTRY:
        described = describe_parser(capture_parser(SCRIPTS / script))
        entries[" ".join(words)] = {"script": script, **described}
    return {"spec": SPEC_VERSION, "entries": entries}


def render_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def load_spec() -> dict[str, Any]:
    """The committed spec, or an empty one when it is missing or unreadable (help then shows less)."""
    try:
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"spec": SPEC_VERSION, "entries": {}}


# --- completion ------------------------------------------------------------------------------------------


def incident_ids(env: dict[str, str] | None = None) -> list[str]:
    """Incidents with local state, from the same directory the scripts use (read-only listing)."""
    env = dict(os.environ) if env is None else env
    base = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sre-harness" / "incidents"
    try:
        return sorted(path.stem for path in base.glob("*.json"))
    except OSError:
        return []


def option_value(line: list[str], flag: str) -> str | None:
    """The value given for a flag earlier on the command line (`--flag value` or `--flag=value`), or None."""
    found = None
    for index, token in enumerate(line):
        if token == flag and index + 1 < len(line):
            found = line[index + 1]
        elif token.startswith(f"{flag}="):
            found = token[len(flag) + 1 :]
    return found


def find_site(line: list[str], env: dict[str, str] | None = None) -> Path | None:
    """The site repository, in the order the scripts use (resolve_site): --site, the environment, the pointer file.

    None for any problem: completion never reports one.
    """
    env = dict(os.environ) if env is None else env
    explicit = option_value(line, "--site")
    candidate: Path | None = None
    if explicit:
        candidate = Path(explicit)
    elif env.get(SITE_ENV):
        candidate = Path(env[SITE_ENV])
    else:
        pointer = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sre-harness" / "site"
        try:
            if pointer.is_dir():
                candidate = pointer
            elif pointer.is_file():
                lines = pointer.read_text(encoding="utf-8").strip().splitlines()
                candidate = Path(lines[0].strip()) if lines and lines[0].strip() else None
        except OSError:
            return None
    if candidate is None:
        return None
    root = candidate.expanduser()
    return root.resolve() if (root / "site.yaml").is_file() else None


def _unquote(value: str) -> str:
    value = value.split(" #", maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _first_document(lines: list[str]) -> list[str]:
    """The content lines of the first YAML document: no blanks or comments, cut at a later `---`."""
    kept: list[str] = []
    for line in lines:
        if line.strip() == "---":
            if kept:
                break
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            kept.append(line)
    return kept


def yaml_child(lines: list[str], parent: str, child: str) -> str | None:
    """The scalar `child:` directly under the top-level `parent:` block, read line by line.

    This is not a YAML parser. It reads what completion needs from files the real loaders have already accepted.
    Deeper keys of the same name are never taken.
    """
    inside = False
    child_indent: int | None = None
    for line in _first_document(lines):
        if _indent(line) == 0:
            inside = line.split(" #")[0].rstrip() == f"{parent}:"
            child_indent = None
        elif inside:
            child_indent = _indent(line) if child_indent is None else child_indent
            if _indent(line) == child_indent and line.strip().startswith(f"{child}:"):
                return _unquote(line.strip()[len(child) + 1 :]) or None
    return None


def yaml_top(lines: list[str], key: str) -> str | None:
    """The scalar value of a top-level `key:` line."""
    for line in _first_document(lines):
        if _indent(line) == 0 and line.startswith(f"{key}:"):
            return _unquote(line[len(key) + 1 :]) or None
    return None


def _read_lines(path: Path, limit: int = 262144) -> list[str]:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.read(limit).splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _files_under(site: Path, relative: str | None, pattern: str) -> list[Path]:
    """Regular files matching a pattern under a directory of the site; never outside it, never through a link."""
    if not relative:
        return []
    base = (site / relative).expanduser()
    try:
        base = base.resolve()
        if not base.is_relative_to(site) or not base.is_dir():
            return []
        return sorted(p for p in base.rglob(pattern) if p.is_file() and not p.is_symlink())[:2000]
    except OSError:
        return []


def site_names(site: Path | None) -> tuple[list[str], list[tuple[str, str]]]:
    """Catalog Component names, and (SLO name, service) pairs, read from the site."""
    if site is None:
        return [], []
    config = _read_lines(site / "site.yaml")
    services: set[str] = set()
    for path in _files_under(site, yaml_child(config, "catalog", "path"), "*.yaml"):
        if path.name.endswith(".slo.yaml"):
            continue
        lines = _read_lines(path)
        name = yaml_child(lines, "metadata", "name")
        if yaml_top(lines, "kind") == "Component" and name:
            services.add(name)
    slos = {
        (name, path.parent.name)
        for path in _files_under(site, yaml_child(config, "slo", "journeys_path"), "*.slo.yaml")
        if (name := yaml_child(_read_lines(path), "metadata", "name"))
    }
    return sorted(services), sorted(slos)


def normalise_words(words: list[str]) -> list[str]:
    """Undo the shells' splitting of '--flag=value' (bash makes three words of it)."""
    words = list(words) or [""]
    if words[-1] == "=":
        words[-1] = ""
    return [w for i, w in enumerate(words) if not (w == "=" and i > 0 and words[i - 1].startswith("-"))]


def _find_option(flag: str, *option_lists: list[dict[str, Any]]) -> dict[str, Any] | None:
    for options in option_lists:
        for option in options:
            if flag in option["flags"]:
                return option
    return None


@dataclass
class Where:
    """What the value being completed can depend on: the command, the words before it, and what is typed of it."""

    group: str
    line: list[str]
    typed: str
    env: dict[str, str] | None


def name_candidates(flag: str, where: Where) -> list[tuple[str, str]]:
    """Service or SLO names read from the site: bare, or `component:<name>` where the command needs that form."""
    services, slos = site_names(find_site(where.line, where.env))
    if flag in SLO_FLAGS:
        service = (option_value(where.line, "--service") or "").removeprefix(SERVICE_PREFIX)
        return [(name, owner) for name, owner in slos if not service or owner == service]
    head, comma, last = where.typed.rpartition(",") if flag == "--services" else ("", "", where.typed)
    listed = {item.removeprefix(SERVICE_PREFIX) for item in head.split(",")}
    prefix = SERVICE_PREFIX if (where.group, flag) in PREFIX_REQUIRED or last.startswith(SERVICE_PREFIX) else ""
    return [(f"{head}{comma}{prefix}{name}", "service") for name in services if name not in listed]


def _value_candidates(flag: str, option: dict[str, Any], where: Where) -> list[tuple[str, str]] | str:
    if option["choices"]:
        return [(choice, "") for choice in option["choices"]]
    if flag in INCIDENT_FLAGS:
        return [(incident_id, "incident") for incident_id in incident_ids(where.env)]
    if flag in PATH_FLAGS:
        return FILES
    if flag in SERVICE_FLAGS or flag in SLO_FLAGS:
        return name_candidates(flag, where)
    return []


def _filter(candidates: list[tuple[str, str]], prefix: str, head: str = "") -> list[tuple[str, str]]:
    return [(head + value, help_text) for value, help_text in candidates if (head + value).startswith(prefix)]


def complete(
    words: list[str], spec: dict[str, Any] | None = None, env: dict[str, str] | None = None
) -> list[tuple[str, str]] | str:
    """Candidates for the last word of a command line (the words after the program name).

    Returns (value, help) pairs, or FILES when the shell should complete file names.
    """
    spec = load_spec() if spec is None else spec
    *prior, cur = normalise_words(words)
    if not prior:
        top = [(g, GROUP_HELP.get(g) or spec["entries"].get(g, {}).get("help", "")) for g in groups()]
        return _filter([*top, ("completion", "Print a shell completion script")], cur)
    if prior[0] == "completion":
        if len(prior) == 1 and not cur.startswith("-"):
            return _filter([(shell, "") for shell in SHELLS], cur)
        return _filter([("--regenerate", "rebuild srectl_spec.json"), ("--check", "fail if it is stale")], cur)
    if prior[0] not in groups():
        return []
    return _complete_group(prior, cur, spec, env)


def _complete_group(
    prior: list[str], cur: str, spec: dict[str, Any], env: dict[str, str] | None
) -> list[tuple[str, str]] | str:
    """Complete inside a known group: its verbs, then the script's own subcommands, flags and values."""
    group = prior[0]
    if verbs(group) and len(prior) == 1:
        return _filter([(v, spec["entries"].get(f"{group} {v}", {}).get("help", "")) for v in verbs(group)], cur)
    found = entry_for(prior)
    entry = spec["entries"].get(" ".join(found[0])) if found else None
    return _complete_inside(entry, prior[len(found[0]) :], cur, env, prior) if found and entry else []


def _complete_inside(
    entry: dict[str, Any], rest: list[str], cur: str, env: dict[str, str] | None, line: list[str]
) -> list[tuple[str, str]] | str:
    commands = entry["commands"]
    sub: str | None = None
    pending: tuple[str, dict[str, Any]] | None = None
    for token in rest:
        if pending:
            pending = None
            continue
        if token.startswith("-"):
            option = _find_option(token, entry["options"], commands[sub]["options"] if sub else [])
            if option and option["value"] and "=" not in token:
                pending = (token, option)
        elif sub is None and token in commands:
            sub = token
    if pending:
        return _value_or_files(pending[0], pending[1], Where(line[0], line, cur, env), cur, "")
    if cur.startswith("--") and "=" in cur:
        flag, _eq, value = cur.partition("=")
        option = _find_option(flag, entry["options"], commands[sub]["options"] if sub else [])
        if not (option and option["value"]):
            return []
        return _value_or_files(flag, option, Where(line[0], line, value, env), cur, f"{flag}=")
    if cur.startswith("-"):
        pool = (commands[sub]["options"] if sub else []) + entry["options"]
        flags = sorted({(flag, option["help"]) for option in pool for flag in option["flags"] if flag.startswith("--")})
        return _filter(flags, cur)
    if sub is None and commands:
        return _filter([(name, info["help"]) for name, info in commands.items()], cur)
    return []


def _value_or_files(
    flag: str, option: dict[str, Any], where: Where, cur: str, head: str
) -> list[tuple[str, str]] | str:
    found = _value_candidates(flag, option, where)
    return found if found == FILES else _filter(found, cur, head)  # type: ignore[arg-type]


BASH = """\
# srectl completion for bash: eval "$(srectl completion bash)"
_srectl_complete() {
    local line out tok pre i
    local -a words
    # COMP_WORDS splits on ':' and '=', which are part of service names and '--flag=value'. Use the line itself.
    line=${COMP_LINE:0:COMP_POINT}
    read -ra words <<<"$line"
    [[ $line == *[[:space:]] ]] && words+=("")
    out=$("$1" __complete "${words[@]:1}" 2>/dev/null)
    COMPREPLY=()
    if [[ $out == ":files" ]]; then
        compopt -o filenames 2>/dev/null
        while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -f -- "${COMP_WORDS[COMP_CWORD]}")
        return 0
    fi
    while IFS= read -r line; do
        [[ -n $line ]] && COMPREPLY+=("${line%%$'\\t'*}")
    done <<<"$out"
    # Bash replaces only the text after its last word break, so drop what comes before it from each candidate.
    tok=${words[${#words[@]}-1]}
    pre=""
    for ((i = ${#tok} - 1; i >= 0; i--)); do
        if [[ $COMP_WORDBREAKS == *"${tok:i:1}"* ]]; then
            pre=${tok:0:i+1}
            break
        fi
    done
    if [[ -n $pre ]]; then
        for i in "${!COMPREPLY[@]}"; do COMPREPLY[i]=${COMPREPLY[i]#"$pre"}; done
    fi
}
complete -F _srectl_complete srectl sre
"""

ZSH = """\
# srectl completion for zsh: eval "$(srectl completion zsh)" (after compinit)
_srectl() {
    local out line val desc
    local -a candidates
    out=$("${words[1]}" __complete "${(@)words[2,CURRENT]}" 2>/dev/null)
    if [[ $out == ":files" ]]; then
        _files
        return
    fi
    for line in "${(@f)out}"; do
        val=${line%%$'\\t'*}
        desc=""
        [[ $line == *$'\\t'* ]] && desc=${line#*$'\\t'}
        candidates+=("${val//:/\\\\:}:${desc}")
    done
    _describe -t srectl 'srectl' candidates
}
(( $+functions[compdef] )) && compdef _srectl srectl sre
"""

FISH = """\
# srectl completion for fish: srectl completion fish | source
function __srectl_complete
    set -l tokens (commandline -opc)
    set -l cur (commandline -ct)
    set -l out ($tokens[1] __complete $tokens[2..-1] "$cur" 2>/dev/null)
    if test "$out" = ":files"
        __fish_complete_path "$cur"
    else
        printf '%s\\n' $out
    end
end
complete -c srectl -f -a '(__srectl_complete)'
complete -c sre -f -a '(__srectl_complete)'
"""

SHELL_SCRIPTS = {"bash": BASH, "zsh": ZSH, "fish": FISH}


def regenerate(check: bool) -> int:
    """Rebuild the spec file, or with check only compare. Re-runs under uv when a script's dependencies are missing."""
    try:
        spec = build_spec()
    except ImportError:
        if os.environ.get("SRECTL_IN_UV"):
            raise
        uv = shutil.which("uv")
        if not uv:
            print("srectl: needs uv, or jsonschema and pyyaml installed, to read the scripts", file=sys.stderr)
            return 1
        os.environ["SRECTL_IN_UV"] = "1"
        args = [uv, "run", "--no-project", "--with", "jsonschema", "--with", "pyyaml", "python", __file__]
        os.execv(uv, [*args, "completion", *(["--check"] if check else ["--regenerate"])])  # noqa: S606
    text = render_spec(spec)
    current = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""
    if check:
        if text != current:
            print("srectl_spec.json is stale: run `srectl completion --regenerate`", file=sys.stderr)
            return 1
        return 0
    SPEC_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {SPEC_PATH}")
    return 0


# --- help and dispatch -----------------------------------------------------------------------------------


def top_help(spec: dict[str, Any]) -> str:
    lines = [
        f"usage: {PROG} <group> [<command>] [options]",
        "",
        "Run the SRE harness scripts. Words after the group go to the script.",
    ]
    section = ""
    seen: set[str] = set()
    for words, _script, sect in REGISTRY:
        if words[0] in seen:
            continue
        seen.add(words[0])
        if sect != section:
            section = sect
            lines += ["", f"{section}:"]
        text = GROUP_HELP.get(words[0]) or spec["entries"].get(words[0], {}).get("help", "")
        lines.append(f"  {words[0]:<12}{text}")
    lines += ["", "Shell:", f"  {'completion':<12}Print a shell completion script (bash, zsh, fish)", ""]
    lines.append(f"Use '{PROG} <group> -h' for a group's commands and options.")
    return "\n".join(lines)


def group_help(group: str, spec: dict[str, Any]) -> str:
    """The commands of a group, for when the script would only say one is required."""
    lines = [f"usage: {PROG} {group} <command> [options]", ""]
    if verbs(group):
        lines.append(GROUP_HELP[group] + ":")
        names = [(v, spec["entries"].get(f"{group} {v}", {}).get("help", "")) for v in verbs(group)]
    else:
        entry = spec["entries"].get(group, {})
        lines.append(entry.get("help", "").rstrip(".") + ":")
        names = [(name, info["help"]) for name, info in entry.get("commands", {}).items()]
    lines += [f"  {name:<12}{text}" for name, text in names]
    lines += ["", f"Use '{PROG} {group} <command> -h' for options."]
    return "\n".join(lines)


def fail(message: str, typed: str, choices: list[str]) -> NoReturn:
    print(f"{PROG}: {message}", file=sys.stderr)
    near = difflib.get_close_matches(typed, choices, n=1)
    if near:
        print(f"did you mean '{near[0]}'?", file=sys.stderr)
    sys.exit(2)


def run_script(script: str, args: list[str]) -> NoReturn:
    uv = shutil.which("uv")
    if not uv:
        print(f"{PROG}: uv is not installed; the scripts run with `uv run`", file=sys.stderr)
        sys.exit(127)
    os.execv(uv, [uv, "run", "--script", str(SCRIPTS / script), *args])  # noqa: S606


def completion_command(args: list[str]) -> int:
    if args in (["--regenerate"], ["--check"]):
        return regenerate(check=args == ["--check"])
    if len(args) == 1 and args[0] in SHELL_SCRIPTS:
        print(SHELL_SCRIPTS[args[0]], end="")
        return 0
    print(f"usage: {PROG} completion {'|'.join(SHELLS)} | --regenerate | --check", file=sys.stderr)
    return 0 if args in (["-h"], ["--help"]) else 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(top_help(load_spec()))
        return 0
    if args[0] == "__complete":
        result = complete(args[1:])
        if result == FILES:
            print(FILES)
        else:
            for value, help_text in result:
                print(f"{value}\t{help_text}" if help_text else value)
        return 0
    if args[0] == "completion":
        return completion_command(args[1:])
    group = args[0]
    if group not in groups():
        fail(f"unknown group '{group}'", group, groups() + ["completion"])
    spec = load_spec()
    if verbs(group):
        if len(args) == 1 or args[1] in ("-h", "--help"):
            print(group_help(group, spec))
            return 0 if len(args) > 1 else 2
        if args[1] not in verbs(group):
            fail(f"unknown command '{args[1]}' for '{group}'", args[1], verbs(group))
    found = entry_for(args)
    if found is None:
        fail(f"no script for '{group}'", group, groups())
    key, script = found
    rest = args[len(key) :]
    if not rest and spec["entries"].get(" ".join(key), {}).get("required"):
        print(group_help(group, spec), file=sys.stderr)
        return 2
    run_script(script, rest)


if __name__ == "__main__":
    sys.exit(main())
