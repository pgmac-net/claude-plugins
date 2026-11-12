# The catalog uses Backstage entity format, without Backstage

Catalog entities are YAML files in git using the Backstage `catalog-info.yaml` model (Component, System, Resource, API, `dependsOn`). We do not run Backstage. An agent reads files well, git gives history and review, and there is nothing to host. Using an existing published model instead of inventing one means the catalog can be loaded into Backstage later without a rewrite.

## Considered options

- **NetBox**: strong for hosts and addresses, weak for service dependencies and reliability data, and one more database to operate.
- **Backstage itself**: a full portal with high upkeep for a small team.
- **Own format**: no lock-in, but no tooling and no migration path either.
