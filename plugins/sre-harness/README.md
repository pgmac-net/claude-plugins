<img src="assets/icon-128.png" alt="SRE harness icon" width="96" align="right">

# sre-harness

A portable harness for doing SRE work with an LLM agent: service catalog, incident triage and remediation, audit, user journeys and SLOs.

So far it has: how a site is described and checked, a service catalog kept in step with the site's own repositories, incident skills that keep a typed timeline, a hook-captured audit log, autonomy tiers with a pre-approved action list, and an optional advisory helper. SLO skills arrive in later releases.

## How it is put together

The plugin holds logic and knows nothing about any site. Everything about one site lives in a private **site repository** that you own. See [`docs/adr/`](docs/adr/) for why, and [`CONTEXT.md`](CONTEXT.md) for the vocabulary.

## Set up a site

1. Copy [`templates/site-repo/`](templates/site-repo/) into a new **private** repository.
2. Edit `site.yaml` and `tiers.yaml`. Never put a secret in them: name the environment variable that holds it.
3. Point the harness at your checkout, one of:
   - `export SRE_HARNESS_SITE=/path/to/site-repo`
   - `ln -s /path/to/site-repo ~/.config/sre-harness/site`
4. Validate:

   ```bash
   uv run scripts/validate_site.py
   ```

   Or ask the agent to "check the site config" (the `site-check` skill).

## The catalog

The catalog is a set of entity files in Backstage format, in two layers that never share a file.

| Layer | Written by | Holds |
|---|---|---|
| `catalog/generated/` | `catalog_sync.py` only | One `Resource` per **deployment unit**: a host, an ArgoCD application, a monitored host |
| everything else under `catalog/` | People, or the `describe-service` skill | **Services** (`Component`), systems, owners: tier, purpose, dependencies, pattern, runbooks |

A service claims its deployment units by listing them under `dependsOn`.

**Sources.** `site.yaml` lists the sources of the generated layer. Each is read from a local git checkout, never from a live system, so sync works during an outage and always produces the same bytes from the same inputs.

| Source type | Reads | Produces |
|---|---|---|
| `ansible-inventory` | An INI inventory: hosts, groups, group children | A `host` Resource per host. Variables, `host_vars` and `group_vars` are never read |
| `argocd-applications` | A directory of areas; a Helm chart area is rendered with `helm template`, a plain one is read as YAML | An `argocd-application` Resource per `Application`, plus one for the cluster |
| `nagios-objects` | Nagios object files | Checks, host groups and parents merged onto the matching host or application; a `monitored-host` Resource for anything unmatched. Check commands are never recorded |

```bash
uv run scripts/catalog_sync.py            # rewrite the generated layer
uv run scripts/catalog_sync.py --check    # exit 1 if it is stale or hand-edited
uv run scripts/catalog_sync.py --check-live   # read-only: declared vs running ArgoCD applications
```

**Drift.** `catalog_check.py` needs only the site repository, so it runs in that repository's CI. `validate_site.py` runs it too.

| Finding | Level |
|---|---|
| Schema violation, duplicate entity, entity in the wrong layer | error |
| Reference to something that does not exist (dangling) | error |
| Service missing what its tier's `catalog_requires` lists | error; a warning if the item is under `sre.unverified` |
| Pattern with no page | error |
| Deployment unit no service reaches, not in `catalog/ignore.yaml` (uncatalogued) | warning |
| Fact drafted but not confirmed (`sre.unverified`), runbook file not found, stale ignore entry | warning |

Ask the agent to "sync the catalog" (`catalog-sync`) or "describe a service" (`describe-service`).

## Incidents

An incident is one **tracking issue**: stable facts in its body, the **timeline** in its comments. Its id is `<repo-alias>-<issue number>`.

```bash
uv run scripts/incident.py open --title "…" --severity P2 --affected "…" --impact "…"   # --drill for practice
uv run scripts/incident.py set --severity P1 --phase Mitigating
uv run scripts/timeline.py add --type action --text "Restarted X, approved by the owner. Errors stopped within a minute."
uv run scripts/timeline.py due        # is a status update owed?
uv run scripts/timeline.py handover --state … --tried … --hypotheses … --next … --holder …
uv run scripts/timeline.py export     # markdown, for the review
uv run scripts/incident.py close      # resolved; the issue stays open for a person to close
```

Timeline entry types: `discovery`, `action`, `decision`, `milestone`, `update`, `handover`, `summary`. Each is one comment with a hidden marker line and a line people read. Comments typed by a person are kept as notes. A marker is honoured only from the tracker repository's owner, members and collaborators, or a login listed under `incident.responders`; from anyone else it is kept as a flagged note, so a stranger commenting on a public tracker cannot forge an entry or its time.

**Audit log.** While an incident is open, a hook records every tool call the session attempts and completes: inputs redacted, file content and tool output as size and SHA-256 only, each line chained to the one before. It records nothing outside an incident and never blocks a tool call. Approvals are added explicitly; agents without hooks self-report, and are marked so.

```bash
uv run scripts/audit.py record --kind approval --text "<blast radius>" --command "<command>" --approver "<who>"
uv run scripts/audit.py verify --check-anchors
uv run scripts/audit.py commit     # to the site's private audit location, on a branch, as a pull request
```

`incident.py close` prints the close-out steps still to do. `audit.py commit` refuses an incident that is not resolved, because committing ends capture and the chat gateway then refuses every approval for it; close it first, or pass `--unresolved` (a person's call, refused from chat). It is safe to run again: it reports `preserved` or `pending`, and refuses a log that changed since it was pushed unless `--amend`. From chat, scripts need `--incident <id>` (`SRE_HARNESS_NO_CURRENT`), because every session shares one current-incident pointer.

The log is spooled under `~/.local/state/sre-harness/` during the incident and committed at the end. A session is bound to an incident by a single-use token that `incident.py` issues. Every timeline entry pins the log's head hash and record count in its marker on the code host, so a later rewrite of the log shows up in `verify --check-anchors`. `commit` refuses a broken chain, a log that disagrees with its pinned hashes, or one with a secret left in it.

What it is not: tamper-proof. The log and the files that switch capture on belong to the same user as the agent, so a process set on defeating them can. The checks make accidents and later edits visible. Off-host shipping of records is the control for a hostile process and is not built yet. Also: the hook fails open, and redaction is pattern-based.

**Autonomy tiers.** Tier 0 is read-only diagnostics, run freely. Tier 1 is any mutation: confirmed by a person after the blast radius is stated. Tier 2 is a mutation the site has pre-approved in its allow-list (`incident.autonomy_file`): an exact command template per service, with a rule for each variable part, a precondition, and a per-incident limit.

```bash
uv run scripts/autonomy.py match --command "<exact command>" --service component:<name>
uv run scripts/autonomy.py run   --command "<exact command>" --service component:<name> --precondition-evidence "<what was observed>"
```

A script decides, by token-for-token match, and the same script executes the command as an argument list with no shell; the agent never types a pre-approved command into a shell. Commands are confined to letters, digits, `. _ : / = @ , + -` and single spaces, so there is nothing for a shell to read differently. The list is empty and switched off by default, and `run` refuses during a drill. Afterwards, `audit.py verify` reports every mutating simple command that ran with no approval for the same words. It judges each command by its program, through carriers such as ssh, `sh -c` and `kubectl exec`; it recognises common mutating commands, not every possible one. See [ADR 0011](docs/adr/0011-tier-two-is-an-exact-match-allow-list.md) and [ADR 0025](docs/adr/0025-mutation-detection-is-structural-per-simple-command.md).

**Advisory helper.** Optional and off by default. When a site enables it (`incident.assist`), a fast judgment model gives a second opinion on three questions: severity, which runbooks to read, and a timeline entry's type.

```bash
uv run scripts/assist.py severity --impact "<what is broken, for whom>"
uv run scripts/assist.py runbooks --symptoms "<what is observed>"
uv run scripts/assist.py entry-type --text "<entry text>"
```

It only advises: it never approves, blocks or selects an action, never decides a tier, never sets a final severity. Enabling it sends text to a third party; secrets are redacted and site identifiers and private addresses are masked first, and nothing is sent if the configured mask file is missing. With no key, a timeout or an error it reports itself unavailable and the incident carries on. See [ADR 0012](docs/adr/0012-the-judgment-model-only-advises.md).

**Cleaning up local state.** During an incident the harness keeps files under `~/.local/state/sre-harness/` (or `$XDG_STATE_HOME/sre-harness/`): the audit spool, a `.closed` marker, the incident's state, session files and bind tokens. They are not removed when the audit log is committed, because the pull request has not merged yet.

```bash
uv run scripts/incident.py gc            # dry run: what would be cleared, and what is kept and why
uv run scripts/incident.py gc --apply    # delete
uv run scripts/incident.py gc --apply --incident <id>   # only this incident; repeat for more
```

`--incident` limits the run to the incidents named: nothing else is assessed or touched, unused bind tokens are left alone, and an id with no local state is listed under `kept` as `no local state`.

An incident is cleared only when all of these hold:

1. Its audit log was committed (the `.closed` marker exists) and it is resolved locally.
2. The audit file on the audit repository's fetched default branch is **byte-identical** to the local spool. The check is the file itself, so a missed trigger leaves files behind and a wrong one cannot delete an unpreserved log.
3. Its tracking issue is closed. (The open-time run cannot ask the code host, so it requires the log to have been committed at least 14 days ago instead.)

An incident whose log was never committed is the only copy of its record: it is reported and never deleted. Deletion is never recursive, never follows a symlink, and only touches files whose names are incident ids inside the state directory. Unused bind tokens older than a week are removed too.

Three triggers, all optional and all safe to miss:

- **At `incident.py open`**: quiet, no network, never fails the open.
- **On demand**: `incident.py gc`.
- **A `post-merge` git hook** in the site repository, which runs `gc --apply` after a pull. It fires only when a pull happens on that machine, which is why it is a convenience and not the safety. Best effort: it always exits 0. Install it by **copying**, so a pull cannot change it:

  ```bash
  cp .githooks/post-merge "$(git rev-parse --git-path hooks)/post-merge"
  git config sre-harness.plugin /path/to/plugins/sre-harness
  ```

  The plugin directory comes only from `SRE_HARNESS_PLUGIN` or that per-clone setting, never from a guess or from the repository's contents, and with neither set the hook does nothing. It runs `uv` from the plugin directory with `--no-config --no-project`, so a pulled `uv.toml` cannot influence it. Do not enable it with `core.hooksPath`: that runs whatever the last pull brought in. Re-copy it when you update the plugin and want a newer hook.

**Site procedures.** The skills say *when*; the site says *how*. `site.yaml` lists procedure files per stage (`triage`, `evidence`, `comms`, `review`) under `incident.procedures`, and the skills read them at that stage. This is where monitoring access, fallbacks, evidence capture and review format live. See [ADR 0010](docs/adr/0010-site-procedures-carry-the-how.md).

**Drills.** `--drill` tracks a practice incident in the private repository named by `incident.drill`, so nothing fake is published. A drill never mutates anything.

Ask the agent to "start an incident" (`start-incident`) or "create a PIR" (`create-pir`).

## Journeys and SLOs

Ask the agent to "define a journey" (`define-journey`) for a service. It writes `<journeys_path>/<service>/<journey>.md`, an OpenSLO file per SLI beside it, and the service's error-budget policy, then generates Prometheus recording rules (`slo.py generate`) for the site to sync into its metrics backend. `slo.py status` reports each SLO's attainment, budget remaining and state (healthy, burning fast, exhausted) from those recorded series, so every reader gets the same numbers. `slo.py dashboard` writes a Grafana dashboard (one row per SLO: attainment, budget left, burn rates, history) from the same definitions, reading only those recorded series; a site syncs it the way it syncs the rules. Design: [ADR 0014](docs/adr/0014-short-slo-windows-and-our-own-rule-generator.md).

**Windows must fit the backend's retention.** Recorded series are kept no longer than raw ones. Grafana Cloud's free tier, measured for the first site on 2026-11-16:

| Limit | Free tier |
|---|---|
| Active metric series | 10,000 |
| Retention (metrics, logs, traces) | 14 days |
| Synthetic Monitoring | 100k API test runs a month |
| Rule groups / rules per group | 35 / 20 by default (not stated per tier) |

So use 7-day windows on it, and keep SLI series few: filter at the scraper so only what the SLIs need is shipped. If usage approaches the series cap, in order: tighten the scraper filters; drop request-ratio SLIs and keep the probes; move SLIs to a separate stack. A Tier 1 service with no journey is a warning in `validate_site.py`; a broken journey or SLO file is an error.

## Error budgets and the reliability report

An error budget has consequences. Each budgeted SLO has a **freeze scope**: its journey's service, plus any services the journey lists in `suspends:`. While the budget is exhausted, every service in the scope is **frozen**: `slo.py status` says so, the agent must state the freeze before asking for a mutation there and record the owner's exception if the change is not itself the fix, and `autonomy.py` keeps the services' pre-approved actions at Tier 1. That check is live, at the moment of the decision, and fails closed: a budget in an unknown state, or a backend that cannot be read, also keeps them at Tier 1. Each refusal is a `tier2_suspended` record in the audit log. Design: [ADR 0015](docs/adr/0015-budget-enforcement-is-checked-live-and-fails-closed.md).

```bash
uv run scripts/budget.py nagios [--slo <name>]   # OK, WARNING burning fast, CRITICAL exhausted (names the freeze), UNKNOWN
uv run scripts/budget.py snapshot --out <dir>     # one line per SLO per day into <dir>/YYYY-MM.jsonl
uv run scripts/report.py --month YYYY-MM --history <dir> --out reports/YYYY-MM.md
```

- **Alerts** come from `budget.py nagios`, run as a Nagios check, so budget alerts share the site's one alerting path. Its own failures are UNKNOWN, never CRITICAL.
- **Snapshots** keep what the backend's retention would lose. A daily run writes yesterday and fills any missing day still inside retention, so a missed run repairs itself. Each line records the day's attainment, the lowest budget, the highest burn rates, the worst state reached and the minutes actually measured. Design: [ADR 0016](docs/adr/0016-budget-history-is-kept-as-daily-snapshots.md).
- **The report** is one markdown file a month with a section for every service whose tier requires an SLA report: attainment (the mean of the daily values), days captured, lowest budget, days burning fast, exhausted and frozen, the month's incidents and recommendations from fixed rules. A service with no journey says so. An incident counts against the services on its `Services:` line (`incident.py set --services`); without one, the catalog names in its `Affected:` line; an incident that matches nothing is listed as unattributed. The report exits 1 when a budgeted SLO lacks more than `--max-missing` days of snapshots, after writing.

## Reliability design

Three tools for learning from incidents before the next one. Design: [ADR 0017](docs/adr/0017-reliability-design-stpa-cast-and-a-ranked-backlog.md).

```bash
uv run scripts/stpa.py check                     # every analysis: schema, references, evidence rules
uv run scripts/stpa.py render <service>          # regenerate the control-structure diagram
uv run scripts/stpa.py findings <service>        # uncontrolled loss scenarios and their issues
uv run scripts/trends.py [--as-of YYYY-MM-DD] [--pirs <dir>] [--json]
```

- **Design review** (skill `design-review`): a targeted STPA analysis of one service. The agent drafts losses and hazards, the control structure, unsafe control actions and loss scenarios from evidence, and a person gates each step. The analysis lives in the site repository at `<analyses_path>/<service>.md`; `validate_site.py` checks it. Each loss scenario no working control handles today becomes a `design-review` issue, after a search for an existing one.
- **CAST** (in `create-pir`): an optional section applying the same control model to one incident. Added on request, offered once when automation acted or failed to act, and it proposes new unsafe actions or scenarios back to the service's analysis.
- **Recommendations backlog** (`trends.py`): rule-ranked from PIR front matter (`services:`, or tags matched against the catalog) and every issue labelled with `reliability.action_labels`, searched across the owners in `repos`. It lists recurring themes, stale action items, repeat incidents with open items, and design reviews due. `report.py` appends it to the monthly report (`--pirs` points at a PIR checkout). Anything it could not read is printed as **Not complete**, never silently left out; to include issues in private repositories, run it with a token that can read them.

**From chat.** With a `chat` section in `site.yaml` and the gateway running, a responder starts an incident with `/incident <text>` in Slack. The agent works it in a private channel. Every command that isn't pre-approved waits for a listed approver's click, which is recorded in the audit log. Setup and the threat model: [gateway/README.md](gateway/README.md).

**From an alert.** With a `runner` section as well, the gateway polls the alert source and starts an **unattended triage** for new problems on services in the chosen tiers. A triage opens a private record, gathers evidence and proposes a fix, and changes nothing. When the first responder speaks, it becomes a normal chat incident, and the responder decides whether to promote it to the tracker or dismiss it. Rules: [gateway/README.md](gateway/README.md#unattended-triage), ADR 0018 and ADR 0019.

## Contents

| Path | Purpose |
|---|---|
| `skills/site-check/` | Finds and validates the site; shared reference for loading site configuration |
| `skills/catalog-sync/` | Regenerates the generated layer, reports drift, proposes a pull request |
| `skills/describe-service/` | Interviews the user and writes one service entry |
| `skills/define-journey/` | Interviews the owner and writes a journey, its OpenSLO definitions and its error-budget policy |
| `skills/start-incident/` | Works a live incident: record, triage, gated recovery, typed timeline |
| `skills/create-pir/` | Writes the post-incident review from the timeline, with an optional CAST section |
| `skills/design-review/` | Targeted STPA analysis of one service, gated step by step; files findings |
| `schemas/` | JSON Schema for `site.yaml` (version 7), journey front matter, STPA analyses, the supported OpenSLO subset, tier definitions, catalog entities and the pre-approved action list |
| `scripts/validate_site.py` | Site validation: schema, paths, references, audit privacy, secret shapes, catalog |
| `scripts/catalog_sync.py` | Source adapters and the generated layer |
| `scripts/catalog_check.py` | Entity validation and drift report |
| `scripts/incident.py` | Opens, updates and resolves the tracking issue; promotes or dismisses an unattended triage |
| `scripts/alerts.py` | Which alert-source problems start an unattended triage; `check` shows what would start now |
| `scripts/timeline.py` | Writes, reads and exports timeline entries; says when a status update is due |
| `hooks/hooks.json`, `scripts/audit_hook.py` | The audit hook: captures tool calls during an incident |
| `scripts/audit.py` | Explicit audit records, verification, commit to the audit location |
| `scripts/autonomy.py` | Decides whether a command is a pre-approved action, and executes it without a shell |
| `scripts/assist.py` | Optional advisory second opinions from a judgment model, with masking |
| `scripts/chat_policy.py` | Tool policy for an agent driven from chat: the read-only allow-list, the deny-list, checks on a site's additions |
| `scripts/slo.py` | Journeys and SLOs: `check`, `generate` (recording rules), `dashboard`, `status` (attainment, budget remaining, budget state, freeze) |
| `scripts/budget.py` | Error-budget enforcement and history: the live Tier 2 gate, the Nagios check, daily snapshots |
| `scripts/report.py` | The monthly reliability report, from snapshots, the catalog and the incident tracker, ending with the recommendations backlog |
| `scripts/stpa.py` | STPA analyses: `check`, `render` (Mermaid diagram), `findings` |
| `scripts/trends.py` | The recommendations backlog: themes, stale action items, repeat incidents, design reviews due |
| `scripts/kube_read.py` | Read-only kubectl for an agent driven from chat: allowed verbs and options only, checked per argument, no shell |
| `scripts/redact.py` | Secret detection and redaction, shared by the validator and the audit log |
| `scripts/check_identifiers.py` | Scans for site identifiers that must not ship in the plugin |
| `gateway/` | The chat-ops gateway: Slack Socket Mode, approval requests, sessions per incident, unattended triage ([gateway/README.md](gateway/README.md)) |
| `AGENTS.md` | How an agent other than Claude Code uses the harness: where things are, the rules that always hold, self-reported audit |
| `adapters/pi/sre-audit.ts` | Pi extension: feeds Pi's tool calls to the audit hook, recording `agent: pi` and the model |
| `docs/supported-agents.md` | Which agents and models have run the alert drill, step by step: the supported floor |
| `templates/site-repo/` | Starter layout for a new site repository |
| `assets/` | The icon: `icon.svg` is the source, `icon-<size>.png` the renders, and `render.py` regenerates them. Slack takes `icon-1024.png` |
| `docs/adr/` | Design decisions |
| `CONTEXT.md` | Glossary |

Scripts need [`uv`](https://docs.astral.sh/uv/); dependencies are declared inline. Catalog sync also needs `git`, and `helm` for chart areas. The incident scripts need the `gh` CLI, signed in.

## Upgrading a site

**From schema version 6 to 7.** Set `schema_version: 7`. The new `runner` section is optional; `runner: {}` means no unattended triage. To add one, it needs a `chat` section, a monitoring source of type `nagios`, and a private `tracker_repo`. Start with `enabled: false` and run `/incident drill alert <host>/<check>` first.

**From schema version 5 to 6.** Set `schema_version: 6`. The new `reliability` section is optional; leave it out or set `reliability: {}` for the defaults. Add `services:` (catalog names) to the front matter of PIRs so the backlog can attribute them.

**From schema version 4 to 5.** Set `schema_version: 5`. `slo: {}` stays valid and means no SLOs; the template shows a filled-in `slo` section.

**From schema version 3 to 4.** Set `schema_version: 4`. `chat: {}` stays valid and means no chat gateway; the template shows a filled-in `chat` section.

**From schema version 2 to 3.** Set `schema_version: 3` and add an `incident` section. Minimum:

```yaml
incident:
  timezone: Etc/UTC          # IANA name
  procedures_path: procedures
```

Add `update_minutes`, `procedures`, `severity_criteria`, `evidence_dir` and `drill` as you write them; the template shows each.

**From version 1 to 2.** Replace `catalog_path: <dir>` with a `catalog` section (`path`, `default_owner`, `sources`), and add `catalog_requires` to tiers to make the catalog check enforce what a service entry must state.

## Keeping site identifiers out

The plugin is public. CI runs `check_identifiers.py` with generic patterns (private addresses, home paths, internal hostnames, chat IDs, tokens). Before proposing a change, also scan against your own site's denylist, which never leaves your private repository:

```bash
uv run scripts/check_identifiers.py . --denylist "$SRE_HARNESS_SITE/site-identifiers.txt"
```

Mark a deliberate example with `identifier-allow` on the same line.

## Development

```bash
cd plugins/sre-harness
uv run --no-project --with pytest --with jsonschema --with pyyaml pytest
ruff check . && ruff format --check .
uv run --no-project --with pylint --with pytest --with jsonschema --with pyyaml pylint scripts tests
```
