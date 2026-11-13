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

The log is spooled under `~/.local/state/sre-harness/` during the incident and committed at the end. A session is bound to an incident by a single-use token that `incident.py` issues. Every timeline entry pins the log's head hash and record count in its marker on the code host, so a later rewrite of the log shows up in `verify --check-anchors`. `commit` refuses a broken chain, a log that disagrees with its pinned hashes, or one with a secret left in it.

What it is not: tamper-proof. The log and the files that switch capture on belong to the same user as the agent, so a process set on defeating them can. The checks make accidents and later edits visible. Off-host shipping of records is the control for a hostile process and is not built yet. Also: the hook fails open, and redaction is pattern-based.

**Autonomy tiers.** Tier 0 is read-only diagnostics, run freely. Tier 1 is any mutation: confirmed by a person after the blast radius is stated. Tier 2 is a mutation the site has pre-approved in its allow-list (`incident.autonomy_file`): an exact command template per service, with a rule for each variable part, a precondition, and a per-incident limit.

```bash
uv run scripts/autonomy.py match --command "<exact command>" --service component:<name>
uv run scripts/autonomy.py run   --command "<exact command>" --service component:<name> --precondition-evidence "<what was observed>"
```

A script decides, by token-for-token match, and the same script executes the command as an argument list with no shell; the agent never types a pre-approved command into a shell. Commands are confined to letters, digits, `. _ : / = @ , + -` and single spaces, so there is nothing for a shell to read differently. The list is empty and switched off by default, and `run` refuses during a drill. Afterwards, `audit.py verify` reports every mutating shell command that ran with no approval for that exact command; that detection is pattern-based. See [ADR 0011](docs/adr/0011-tier-two-is-an-exact-match-allow-list.md).

**Advisory helper.** Optional and off by default. When a site enables it (`incident.assist`), a fast judgment model gives a second opinion on three questions: severity, which runbooks to read, and a timeline entry's type.

```bash
uv run scripts/assist.py severity --impact "<what is broken, for whom>"
uv run scripts/assist.py runbooks --symptoms "<what is observed>"
uv run scripts/assist.py entry-type --text "<entry text>"
```

It only advises: it never approves, blocks or selects an action, never decides a tier, never sets a final severity. Enabling it sends text to a third party; secrets are redacted and site identifiers and private addresses are masked first, and nothing is sent if the configured mask file is missing. With no key, a timeout or an error it reports itself unavailable and the incident carries on. See [ADR 0012](docs/adr/0012-the-judgment-model-only-advises.md).

**Site procedures.** The skills say *when*; the site says *how*. `site.yaml` lists procedure files per stage (`triage`, `evidence`, `comms`, `review`) under `incident.procedures`, and the skills read them at that stage. This is where monitoring access, fallbacks, evidence capture and review format live. See [ADR 0010](docs/adr/0010-site-procedures-carry-the-how.md).

**Drills.** `--drill` tracks a practice incident in the private repository named by `incident.drill`, so nothing fake is published. A drill never mutates anything.

Ask the agent to "start an incident" (`start-incident`) or "create a PIR" (`create-pir`).

## Contents

| Path | Purpose |
|---|---|
| `skills/site-check/` | Finds and validates the site; shared reference for loading site configuration |
| `skills/catalog-sync/` | Regenerates the generated layer, reports drift, proposes a pull request |
| `skills/describe-service/` | Interviews the user and writes one service entry |
| `skills/start-incident/` | Works a live incident: record, triage, gated recovery, typed timeline |
| `skills/create-pir/` | Writes the post-incident review from the timeline |
| `schemas/` | JSON Schema for `site.yaml` (version 3), tier definitions, catalog entities and the pre-approved action list |
| `scripts/validate_site.py` | Site validation: schema, paths, references, audit privacy, secret shapes, catalog |
| `scripts/catalog_sync.py` | Source adapters and the generated layer |
| `scripts/catalog_check.py` | Entity validation and drift report |
| `scripts/incident.py` | Opens, updates and resolves the tracking issue |
| `scripts/timeline.py` | Writes, reads and exports timeline entries; says when a status update is due |
| `hooks/hooks.json`, `scripts/audit_hook.py` | The audit hook: captures tool calls during an incident |
| `scripts/audit.py` | Explicit audit records, verification, commit to the audit location |
| `scripts/autonomy.py` | Decides whether a command is a pre-approved action, and executes it without a shell |
| `scripts/assist.py` | Optional advisory second opinions from a judgment model, with masking |
| `scripts/redact.py` | Secret detection and redaction, shared by the validator and the audit log |
| `scripts/check_identifiers.py` | Scans for site identifiers that must not ship in the plugin |
| `templates/site-repo/` | Starter layout for a new site repository |
| `docs/adr/` | Design decisions |
| `CONTEXT.md` | Glossary |

Scripts need [`uv`](https://docs.astral.sh/uv/); dependencies are declared inline. Catalog sync also needs `git`, and `helm` for chart areas. The incident scripts need the `gh` CLI, signed in.

## Upgrading a site

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
