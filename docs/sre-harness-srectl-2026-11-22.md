# sre-harness 0.24.0: srectl, a terminal front end with shell completion (homelabia#282)

## Problem

Running a harness script meant `uv run ~/pgmac/claude-plugins/plugins/sre-harness/scripts/<script>.py …`. Two shell shims (`~/bin/sre-incident`, `~/bin/sre-slo`) covered two of sixteen scripts, with no grouping and no completion. The scripts are many, and several have their own subcommands (`incident`, `timeline`, `audit`, `slo`, …), so a person had to remember which script held which verb.

## Change

- New `scripts/srectl.py`: one standard-library file, run as `srectl` (or `sre`). `srectl <group> [<command>] [options]` replaces itself with `uv run --script <script> …`, so exit codes, stdin and the terminal are the script's own.
- Groups follow the topic, not the file: `incident`, `timeline`, `audit`, `autonomy`, `assist`, `alerts`, `slo`, `budget`, `stpa`, `report`, `trends`, plus `catalog check|sync` and `site check` (three scripts behind two groups). `audit_hook.py`, `kube_read.py` and `check_identifiers.py` are plumbing and stay hidden.
- `srectl` alone lists the groups; `srectl <group>` lists its commands (exit 2 when the script would require one). An unknown group or verb exits 2 and suggests the nearest name.
- `srectl completion bash|zsh|fish` prints a completion script that registers both names. All three call `srectl __complete`, which reads the committed `scripts/srectl_spec.json`, so a TAB needs no `uv` and no imports of the scripts' dependencies.
- The spec is generated from each script's real `argparse` parser (`srectl completion --regenerate`). It records subcommands, flags, whether a flag takes a value, `choices` and help text. `--check` and a pytest fail when it is stale.
- Values complete for `--incident` (local incident state, read-only listing of `$XDG_STATE_HOME/sre-harness/incidents`), for `choices`, and for `--site`/`--out` (the shell completes files). Both `--flag value` and `--flag=value` work; bash's three-word split of the second form is undone.
- Docs: README "Command line" section, `plugins/sre-harness/docs/srectl.md` (install, completion per shell, regenerating), `AGENTS.md` rule, `CONTEXT.md` term **srectl**.

## Decisions (grilled on the ticket)

1. Name: `srectl`, with an `sre` symlink. Completion registers both.
2. A file in `scripts/`, not a package: the plugin deliberately is not one. It finds the scripts through its resolved real path, so a `~/bin` symlink works.
3. Curated groups, not one-to-one script names. Script options pass through untouched, so the scripts stay the source of truth.
4. Completion data is a generated spec with a freshness test, not live `--help` parsing (about 130 ms a TAB, fragile) and not a hand-kept list (shallow).
5. Values: incident ids and paths now; service and SLO names later (needs the site parsed at TAB time).
6. Shells: bash, zsh, fish.
7. "Interactive" means help, completion and clear errors. No prompting and no TUI in this version.
8. **People only.** The chat policy allow-list and the audit log's `HARNESS_SCRIPT` pairing match `uv run <abs path>/scripts/<x>.py`. Skills and agents keep that form; AGENTS.md says not to run `srectl`. No policy or audit code changed.
9. Install is documented steps, not an installer. 0.24.0, with no sre-catalog pin bump because no rule changed.

## Checks

- `ruff check`, `ruff format --check` and `pylint scripts tests` all exit 0; the full suite passes (918 passed, 1 skipped: fish is not installed on the build host).
- `check_identifiers.py` is clean. `srectl completion --check` is clean.
- Completion was exercised in a live bash session and in a real zsh driven through tmux (candidates with help text, choices, unique-prefix completion). The fish script is only syntax-checked when `fish` is present, so it has not been run.

## Not done

Prompting for missing flags, a menu or TUI, completion of service and SLO names, and use by agents. The first and third are follow-ups.
