# Journey files

Everything for one service lives in `<journeys_path>/<service>/`, where `<service>` is the catalog name of the Component.

| File | What |
|---|---|
| `<journey>.md` | The journey: YAML front matter, then prose |
| `<journey>.slo.yaml` | One OpenSLO v1 `SLO` per SLI; more than one file may name the same journey (`<journey>-requests.slo.yaml`) |
| `error-budget-policy.md` | What each budget state changes for this service |

Schemas: `<plugin root>/schemas/journey.schema.json` and `openslo.schema.json`.

## Journey front matter

```yaml
---
title: Open an internal web app
service: component:ingress
owner: user:owner
steps:
  - name: The name resolves
    depends_on: [component:dns]
    measured_by: canary probe (fails if the name does not resolve)
  - name: Ingress accepts the connection and completes TLS
    depends_on: [component:ingress]
    measured_by: canary probe
  - name: The request is routed to a backend and answered
    depends_on: [component:cluster-control-plane]
    measured_by: canary probe
error_budget_policy: error-budget-policy.md
---
```

The prose below it says who the user is, why the journey matters, what is deliberately not measured, and when the target is next reviewed.

## SLO: a probe that spends budget

```yaml
apiVersion: openslo/v1
kind: SLO
metadata:
  name: ingress-open-internal-web-app
  displayName: Open an internal web app
  annotations:
    sre-harness/journey: open-internal-web-app
    sre-harness/expected-interval: 60s
spec:
  service: ingress
  description: Share of canary probes from outside the cluster that succeed.
  budgetingMethod: Occurrences
  timeWindow:
    - duration: 7d
      isRolling: true
  objectives:
    - target: 0.995
  indicator:
    metadata:
      name: canary-probe
    spec:
      ratioMetric:
        counter: false
        good:
          metricSource:
            type: Prometheus
            spec:
              query: probe_success{job="sre-probe-canary"}
        total:
          metricSource:
            type: Prometheus
            spec:
              query: probe_success{job="sre-probe-canary"}
```

For a gauge, `total` is the same selector; the expected count comes from `expected-interval`. A probe that stops reporting counts as failing.

## SLO: a request ratio, recorded but not budgeted

```yaml
metadata:
  name: ingress-requests
  annotations:
    sre-harness/journey: open-internal-web-app
    sre-harness/budget: "false"
spec:
  indicator:
    spec:
      ratioMetric:
        counter: true
        good:
          metricSource: {type: Prometheus, spec: {query: 'nginx_ingress_controller_requests{status!~"5.."}'}}
        total:
          metricSource: {type: Prometheus, spec: {query: nginx_ingress_controller_requests}}
```

No traffic in five minutes counts as no errors.

## What the rules record

Per SLO, labelled `slo`, `service`, `journey` and `budget`:

| Series | Meaning |
|---|---|
| `slo:sli_error:ratio_rate5m` | Share of bad events in the last five minutes |
| `slo:sli_error:ratio_rate1h`, `…6h`, `…1d`, `…<window>` | Its average over each window |
| `slo:objective:ratio` | The target |
| `slo:error_budget:remaining_ratio` | 1 − error ratio over the window ÷ (1 − target); below 0 means exhausted |

`slo.py status` reads these and reports a state: **healthy**; **burning fast** (1-hour burn over 14.4 or 6-hour burn over 6); **exhausted** (no budget left); **unknown** (no data); **informational** (not budgeted).

## Error-budget policy

```markdown
# Error-budget policy: <service>

| State | What changes |
|---|---|
| Healthy | Normal change. Risky changes and experiments are fine. |
| Burning fast | An alert fires. Look before making further changes. |
| Exhausted | Freeze risky changes to <service> and its dependencies. Switch Tier 2 actions off for them. Reliability work first until the budget recovers. |

Exceptions: <who can override, and how it is recorded>.
Review: <when the target and this policy are next looked at>.
```
