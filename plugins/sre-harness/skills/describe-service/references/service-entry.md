# Service entry reference

A service is a Backstage `Component`. Harness facts sit under `spec.sre`. Schema: `<plugin root>/schemas/entity.schema.json`.

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: shop                 # lower-case, digits, hyphens
  title: Shop
  description: One line a newcomer would understand.
spec:
  type: service
  lifecycle: production      # experimental | production | deprecated
  owner: user:someone        # must exist under owners/
  system: storefront         # optional; must exist under systems/
  dependsOn:
    - resource:app-shop      # deployment units it is made of
    - component:ingress      # services it needs
  sre:
    tier: 1
    purpose: Customers cannot place orders.
    pattern: argocd-helm-app # a page in the patterns directory
    runbooks:
      - shop-checkout-stuck.md
    monitoring:
      - "edge: HTTP shop"    # only checks not already on its Resources
    evidence:
      - claim: Depends on ingress
        source: "PIR 2026-01-06: shop unreachable while ingress was down"
    unverified:
      - dependencies         # drafted, not yet confirmed by a person
    notes: Anything a responder should know that fits nowhere else.
```

## Refs

`<kind>:<name>`: `resource:`, `component:`, `system:`, `user:`, `group:`. No namespaces.

## What a tier requires of the entry

Set per tier by `catalog_requires` in the tier definitions. Each item is satisfied as follows:

| Item | Satisfied when |
|---|---|
| `dependencies` | `dependsOn` is not empty |
| `pattern` | `sre.pattern` is set |
| `monitoring` | A Resource it depends on has checks, or `sre.monitoring` is not empty |
| `runbooks` | `sre.runbooks` is not empty |

A missing required item is an error unless it is listed under `sre.unverified`, which turns it into a warning. `unverified` may also list `purpose` and `tier`.

## Systems and owners

```yaml
apiVersion: backstage.io/v1alpha1
kind: System
metadata:
  name: storefront
  description: Everything customers buy through.
spec:
  owner: user:someone
```

```yaml
apiVersion: backstage.io/v1alpha1
kind: User
metadata:
  name: someone
spec:
  memberOf: []
```
