# The catalog has two layers that never share a file

Sync writes one generated Resource per deployment unit into `generated/` and rewrites that directory wholesale on every run. People write services, systems and owners everywhere else, and sync never touches those. A service claims its deployment units by reference. We rejected one file per entity with machine-owned keys, because round-tripping YAML reformats hand edits and ownership bugs silently destroy either the human's facts or the machine's. We rejected hand-written entities only, because nothing would then reveal that the estate had changed.

Full regeneration makes sync idempotent by construction: the same sources always produce the same bytes. Drift is not something sync repairs; it is the reported mismatch between the layers.

## Consequences

- Sync reads git checkouts, never live systems, so it works during an outage and its output does not change with transient state. Comparing against running systems is a separate read-only report.
- Deployment units and services are different entities (Resource and Component). A service made of several units, and a unit that is no service, are both expressible.
- Sync adapters record only what is safe to keep: no inventory variables and no check commands, either of which can carry credentials.
- A hand edit inside `generated/` is lost on the next run and is flagged as stale before then.
