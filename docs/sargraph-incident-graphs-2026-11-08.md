# sargraph: SAR resource graphs in start-incident and create-pir

**Ticket:** [pgmac-net/claude-plugins#19](https://github.com/pgmac-net/claude-plugins/issues/19)
**Branch:** `19-sargraph-graphs`
**Notion:** [claude-plugins#19: sargraph SAR graphs in start-incident and create-pir](https://app.notion.com/p/412524b4a0778172856bf43d002a433f)
**Tool:** [pgmac-net/sargraph](https://github.com/pgmac-net/sargraph), which graphs sysstat SAR data (binary `saDD` or `sar -A` text) to png, svg or jpg

## What changed

Incidents involving resource utilisation or exhaustion (OOM, load, iowait, disk or network saturation) previously had no visual evidence. The PIR described numbers in prose, and the [2026-08-06 dqlite storm PIR](https://github.com/pgmac-net/incidents/blob/main/src/incidents/2026-08-06-dqlite-storm-iscsi-ro-volumes-detection-gap.md) could not confirm its CPU-starvation theory because no SAR history existed. Both skills now use `sargraph` to produce SVG graphs as evidence.

### `start-incident` 1.0.0 → 1.1.0

- New `references/sar-graphs.md`, shared with `create-pir`, covers:
  - when a graph adds value and when to skip it
  - fetching SAR data. It prefers a `sar -A` text export over SSH, which avoids sysstat binary-version mismatches and the need for a local `sar` for binary files.
  - finding selectors with `sargraph list`. Columns use sar's own names (`cpu.%usr`, not `%user`), so `list` comes first.
  - a metric table by symptom
  - `graph` usage: layouts, `--from`/`--to`, titles, the `{metric}` placeholder that `--layout separate` needs
  - how to record the findings in the timeline
- Phase 2 copies SAR files early, before rotation drops the pre-incident baseline.
- Phase 4 graphs to SVG when utilisation or exhaustion is in question.
- Phase 5 regraphs across the fix to capture recovery.
- `sar`/`sargraph` are added to the read-only diagnostics allowed without confirmation.
- Evidence stays outside any repo during the incident: `~/incident-evidence/incidents-<N>/`.

### `create-pir` 1.6.0 → 1.7.0

- Step 1 collects the incident's SVGs. If exhaustion was part of a chain and no graphs exist, it makes them, or records the missing SAR data as an action item.
- Step 4 has a new "Resource graphs" section:
  - SVGs go in `src/incidents/img/<pir-filename-stem>/`
  - the window runs baseline → onset → recovery
  - each graph sits under the Infinite How's level it supports, or in recovery
  - alt text and caption state the finding with AEST times
- Step 9 stages the `img/` directory and adds a PR test-plan line.
- The quality checklist gains two graph items.

Plugin `pgmac-workflows` 1.9.0 → 1.10.0.

## Verified

- Every `sargraph` command in the reference was run against local sysstat data, binary and `sar -A` text: multi-file stitching, globs, `combined`, `separate` with `{metric}`, and ISO `--from`/`--to`. The first draft used `cpu.%user@all`, which failed with `no data matches`. That is why the reference says to run `list` first.
- `mkdocs build --strict` on a copy of the incidents repo: an embedded `img/<stem>/x.svg` builds cleanly, and a missing one aborts the build. The reference's claim that a broken image path is caught in Step 9 therefore holds.

## Decisions

1. **One shared reference, not two copies.** `create-pir` links to `start-incident/references/sar-graphs.md` and keeps only the PIR-specific embedding guidance. This follows the existing cross-skill reference pattern (`incident-issue.md` → `github-issues-setup.md`).
2. **SVG only for committed graphs.** PNG is suggested only for viewing in-session, because the Read tool renders PNG, not SVG.
3. **Missing SAR data never blocks.** It becomes a timeline note and a PIR action item.
