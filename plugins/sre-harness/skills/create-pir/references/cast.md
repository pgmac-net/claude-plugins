# CAST: an optional lens on one incident

CAST (Causal Analysis based on System Theory, Leveson) applies the STPA control model to an incident that has already happened. Infinite How's follows one chain of causes down to a systemic root cause; CAST looks across the control structure and asks, for every controller involved, why its action made sense to it at the time. Use both: CAST sits beside the root-cause chains, it does not replace them.

## When

- **Off by default.** Add it when the user asks ("with CAST", "add a CAST section").
- **Offer it once** when the timeline shows an automated controller acting, or failing to act, during the incident: a self-heal or event handler, a watchdog, a GitOps controller's self-heal, a scheduled job, a Kubernetes controller doing something nobody expected. Say why you are offering it in one sentence. If the user declines, do not ask again for this review.

## How

1. **Start from the service's STPA analysis** if it has one (`<analyses_path>/<service>.md`, see the `design-review` skill). Reuse its controllers, control actions and feedback ids so the two stay joined. Without one, sketch only the controllers this incident involved.
2. **The proximate events.** In two or three lines: what physically happened, and the hazard state the system reached.
3. **Per controller involved** (people, automation, software), a short block:
   - **Responsibilities**: what it was meant to keep true.
   - **Unsafe control actions**: what it did or did not do, in which context. Use the four STPA guidewords.
   - **Process model flaws**: what it believed that was not true, and why that belief was reasonable given what it knew.
   - **Feedback**: what it was told, what it was not, and how late.
   - **Context**: pressures, defaults, timing and changes that made the action seem right.
4. **Coordination**: where two controllers acted on the same thing, or one assumed the other would.
5. **What the structure needs**: changes to controls or feedback, not to people. Each becomes an action item through the normal step, with the same priority rules.

Do not judge a person's action by its outcome. If a reasonable responder with the same feedback would have done the same, the gap is in the feedback or the design.

## Feeding the STPA analysis

Anything CAST found that the analysis does not already have (a new UCA, a scenario, a missing feedback path, a controller not in the structure) is proposed back to it, in the same site-repository change set as the audit log or in its own pull request:

```yaml
proposed:
  - kind: uca          # uca | scenario | feedback | controller
    text: Handler remounts while a pod in another mount namespace still writes
    source: 2026-11-15-example-incident.md
```

`stpa.py check` warns while proposals wait, and `trends.py` lists the service under "Design reviews due". The next `design-review` run takes them in. If the service has no analysis, do not create one from a PIR: say in the PIR that a design review would cover it, and the backlog will raise it once the theme recurs.

## Where it goes in the document

A `## CAST analysis` section after the root-cause chains, as the site's PIR template places it. If the template has no such section, put it after the root causes and say it is optional.
