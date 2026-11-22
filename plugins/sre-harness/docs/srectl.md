# srectl

A command-line front end for the harness scripts. It saves typing `uv run <plugin root>/scripts/<script>.py` and adds tab completion. It adds no behaviour: it runs the script you name with the words you give.

**For people.** Agents and skills run the scripts by full path, because the chat gateway's allow-list and the audit log's pairing of approvals with commands match that form (`uv run <abs path>/scripts/<script>.py`). A command typed in your own terminal is not captured by the audit hook either way.

## Install

`srectl` is one standard-library Python file. Link it onto your `PATH`; it resolves its real path to find the scripts, so a symlink works:

```bash
ln -s <plugin root>/scripts/srectl.py ~/bin/srectl
ln -s <plugin root>/scripts/srectl.py ~/bin/sre      # optional short name
```

It needs `python3` (3.11 or later) and [`uv`](https://docs.astral.sh/uv/), the same as the scripts. If you used the plugin from Claude Code's plugin cache, link to that checkout's path and re-link after an upgrade moves it.

## Completion

Every shell calls `srectl __complete`, which reads `scripts/srectl_spec.json` (commands, flags, choices) and, for names, the site. It needs no `uv`, so a TAB is fast.

If you saved a completion script to a file rather than using `eval`, regenerate it after an upgrade that changes it. 0.24.1 changed the bash one (it now reads the whole line, so `component:` and `--flag=value` complete correctly).

| Shell | Setup |
|---|---|
| zsh | add `eval "$(srectl completion zsh)"` to `~/.zshrc`, after `compinit` |
| bash | add `eval "$(srectl completion bash)"` to `~/.bashrc`, or save the output as `~/.local/share/bash-completion/completions/srectl` |
| fish | `srectl completion fish > ~/.config/fish/completions/srectl.fish` |

Each registers both `srectl` and `sre`. zsh and fish show each candidate's help text.

What completes:

- groups, then the group's commands (for `catalog` and `site`, their verbs)
- flags, from the command and from its group (for example `--site` after `audit record`)
- a flag's fixed choices (`audit record --kind <TAB>`)
- `--incident`: the incidents with local state in `$XDG_STATE_HOME/sre-harness/incidents` (default `~/.local/state`)
- `--site` and `--out`: file names
- `--service`, `--services` and `--slo`: names read from the site (below)

### Service and SLO names

Names come from the site, read on every TAB: the catalog's `Component` entities for services, and the `*.slo.yaml` files for SLOs. Nothing is cached, so a new service completes as soon as its file exists, and nothing from the site is stored in `srectl_spec.json` (the plugin is public).

- The site is found in the scripts' order: a `--site` earlier on the line, then `SRE_HARNESS_SITE`, then `~/.config/sre-harness/site`. With no site, or a site.yaml with no `catalog: path:`, nothing completes and no error is shown.
- `--service` offers bare names. `autonomy match|run --service` requires `component:<name>`, so it offers that form. Type `component:` anywhere else and you get the prefixed names. In `autonomy`, start with `c` (or `component:`): a bare name has no match there.
- `--slo` offers the SLOs of the `--service` already on the line, or all SLOs, each shown with its service, when there is none.
- `--services` takes a comma-separated list. It completes the part after the last comma and leaves out names already listed: `--services ingress,d<TAB>` → `ingress,dns`. It adds no trailing comma.

The scan reads only a site's `catalog.path` and `slo.journeys_path` (from `site.yaml`), stays inside the site, and does not follow links. It is a line scanner, not a YAML parser, and a test holds it to the real loaders.

## Commands

| Group | Script | What it does |
|---|---|---|
| `incident` | `incident.py` | open, attach, status, set, close, promote, dismiss, gc |
| `timeline` | `timeline.py` | add, handover, list, due, export |
| `audit` | `audit.py` | record, show, verify, commit |
| `autonomy` | `autonomy.py` | list, match, run |
| `assist` | `assist.py` | severity, runbooks, entry-type |
| `alerts` | `alerts.py` | check |
| `slo` | `slo.py` | check, generate, dashboard, status |
| `budget` | `budget.py` | nagios, snapshot |
| `stpa` | `stpa.py` | check, render, findings |
| `report` | `report.py` | the monthly reliability report |
| `trends` | `trends.py` | the recommendations backlog |
| `catalog check` | `catalog_check.py` | validate the catalog and report drift |
| `catalog sync` | `catalog_sync.py` | regenerate the generated layer |
| `site check` | `validate_site.py` | validate the site repository |
| `completion` | (srectl itself) | `bash`, `zsh`, `fish`, `--regenerate`, `--check` |

Not listed, because they are plumbing rather than commands: `audit_hook.py` (the hook), `kube_read.py` (the chat gateway's read-only kubectl), `check_identifiers.py` (CI).

`srectl` alone lists the groups. `srectl <group>` lists a group's commands. `srectl <group> <command> -h` is the script's own help. A mistyped group or verb exits 2 and suggests the nearest name. Exit codes, stdin and the terminal are the script's own, because `srectl` replaces itself with `uv run --script <script> …`.

## Changing a script's options

The completion data is generated from each script's real `argparse` parser, so it follows the scripts. After you add or change a flag or subcommand:

```bash
srectl completion --regenerate     # rewrites scripts/srectl_spec.json; commit it
srectl completion --check          # exit 1 when it is stale (the test suite does this too)
```

`--regenerate` needs the scripts' dependencies; when they are not installed it re-runs itself under `uv run --with jsonschema --with pyyaml`.

A new script that is a command people run goes in `REGISTRY` in `scripts/srectl.py`. One that is plumbing goes in `HIDDEN` in `tests/test_srectl.py`. A test fails until you choose.

## Not in this version

Prompting for missing flags, a menu or TUI, and use by agents.
