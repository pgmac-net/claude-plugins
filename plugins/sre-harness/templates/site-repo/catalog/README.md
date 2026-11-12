# Catalog

Entities in Backstage `catalog-info.yaml` format, in two layers that never share a file.

| Path | Written by | Holds |
|---|---|---|
| `generated/<source>/` | `catalog_sync.py` only | One `Resource` per deployment unit found in a source: hosts, ArgoCD applications, monitored hosts. Rewritten wholesale on every sync |
| `services/` | People, or the `describe-service` skill | One `Component` per service: tier, purpose, what it depends on, pattern, runbooks |
| `systems/` | People | `System` entities that group services |
| `owners/` | People | `User` and `Group` entities |
| `ignore.yaml` | People | Generated Resources that deliberately belong to no service, each with a reason |

Refs are `<kind>:<name>`, for example `resource:host-web01` or `component:ingress`.

A service claims its deployment units by listing them under `spec.dependsOn`.
`catalog_check.py` reports drift between the layers: Resources no service
reaches, references to things that no longer exist, and services missing what
their tier requires.
