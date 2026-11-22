# sre-harness 0.24.1: srectl completes service and SLO names (claude-plugins#65)

## Problem

`srectl` (0.24.0, homelabia#282) completed commands, flags, choices, `--incident` and file paths, but not `--service`, `--services` or `--slo`. Those names live in the site catalog, and `srectl.py` is stdlib-only with no dependencies, so it cannot use the scripts' YAML loaders. Site content must also stay out of the committed `srectl_spec.json`, because the plugin is public.

## Change

- `srectl` reads the names from the site on every TAB, with no cache:
  - **Services**: files under `catalog.path` whose top-level `kind:` is `Component`, named by `metadata.name` (the set `stpa.catalog_services` builds).
  - **SLOs**: `*.slo.yaml` under `slo.journeys_path`, named by `metadata.name`, with the parent directory as the service.
- The reader is a line scanner (`yaml_child`, `yaml_top`), not a YAML parser. It takes only a direct child of a top-level key (so `catalog.sources[].path` is never mistaken for `catalog.path`), reads the first document only, and handles quotes and trailing comments. It stays inside the site, does not follow links, and reads at most 2,000 files of 256 KB.
- The site is found in `resolve_site`'s order: a `--site` earlier on the line (both `--site x` and `--site=x`), `SRE_HARNESS_SITE`, then `~/.config/sre-harness/site`. Any problem gives no candidates and no error.
- Forms: bare names by default; `component:<name>` for `autonomy match|run --service`, which rejects bare names; and once `component:` is typed, prefixed names for any command.
- `--slo` narrows to the `--service` already on the line, or lists every SLO with its service as help text.
- `--services` completes the part after the last comma, keeps the earlier names, and leaves out names already listed.
- bash: the function now reads `COMP_LINE`, not `COMP_WORDS`, because bash splits `component:x` and `--flag=value` at `:` and `=`. It trims from each candidate the part bash will not replace. zsh and fish needed no change.

## Decisions (grilled on the ticket)

1. A live narrow scan, not an auto-refreshing or explicit cache: names are always current, and there is nothing to invalidate.
2. Bare names where either form is accepted, prefixed where required, switching on typing `component:`.
3. `--slo` narrows by `--service`.
4. `--services` completes the last list element.
5. Site lookup mirrors the scripts and fails silently.
6. Patch release: 0.24.1.

## Checks

- `ruff check`, `ruff format --check` and `pylint scripts tests` exit 0; the full suite passes (935 passed, 1 skipped: fish is not installed on the build host). `check_identifiers.py` and `srectl completion --check` are clean.
- The scanner was held to the real loaders: on the shipped template site in a test, and on a real site (13 of 13 services and 8 of 8 SLOs matched).
- Run live in bash (as bash calls the function, with its word breaks) and in a real zsh through tmux. The fish script is unchanged and is still untested.

## Not done

Prompting for missing flags, a menu or TUI, and use by agents. In `autonomy`, a bare name (no `component:`) matches nothing, because the command itself rejects it.
