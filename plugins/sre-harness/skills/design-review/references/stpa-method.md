# STPA, as this harness uses it

STPA (System-Theoretic Process Analysis, Leveson) treats reliability as a control problem: losses happen when controllers act unsafely on a process, or fail to act, because their model of the process is wrong or their feedback is missing. It finds design gaps before they become incidents, which is why it is aimed at Tier 1 services whose incident history shows detection gaps and remediation that did not land.

## Step 1: losses and hazards

- A **loss** is something the owner cannot accept: data lost, a service unavailable to its users, a person needed out of hours, a security exposure. Say it in their terms. IDs `L-1`, `L-2`…
- A **hazard** is a system state that, with worst-case conditions, leads to a loss. Not a cause, not a failure of one part: a state. "A volume is mounted read-only while its application writes to it." IDs `H-1`…; each lists the losses it can lead to.
- Keep it short. Five losses is a lot; most services need two or three.

## Step 2: the control structure

| Element | What it is | Example |
|---|---|---|
| Controller (`C-n`) | Anything that decides and acts: a person, an automated handler, a software controller | Operator; Nagios event handler; Kubernetes controller manager |
| Responsibilities | What it is meant to keep true | "Remount stale volumes without data loss" |
| Process model | What it believes about the process, and from what | "A volume with no open writers is safe to remount" |
| Controlled process (`P-n`) | What is acted on | Jiva replica set; NFS export |
| Control action (`CA-n`) | One thing a controller can do to a process or to another controller | "Delete the controller pod"; "Disable the handler" |
| Feedback (`F-n`) | What tells a controller the state of what it controls | A Nagios check; a log line; a person noticing |

Controllers can control other controllers: a person enables or disables an automated handler, GitOps reverts a hand change. Draw those too; conflicting controllers are a classic cause.

Mark feedback `present`, `inadequate` (exists but late, partial or misleading) or `missing` (the controller needs it and has none). An automated controller with no working feedback is a warning `stpa.py check` raises on its own.

## Step 3: unsafe control actions

For each control action, ask all four:

| Guideword (`type`) | Question |
|---|---|
| `not-provided` | Is there a context where not doing it leads to a hazard? |
| `provided-unsafe` | Is there a context where doing it leads to a hazard? |
| `wrong-timing` | Too early, too late, or in the wrong order? |
| `wrong-duration` | Stopped too soon, or kept up too long (applies to continuous actions, retries, loops)? |

A UCA is a sentence: *controller* *does or does not do action* *in context*, leading to *hazard*. The context is the important part: "remounts a volume **while a pod in another mount namespace still holds it**".

Evidence: cite the PIR, runbook, issue or source line. If none exists, set `hypothesised: true`. Never cite something you did not read.

## Step 4: loss scenarios

For each UCA, a scenario says how it happens. Causal factors (`causal_factors`):

| Factor | Typical form here |
|---|---|
| `flawed-process-model` | The handler's guard checks the wrong namespace; the runbook assumes a state that changed |
| `missing-feedback` | Nothing reports that remediation ran and failed |
| `inadequate-feedback` | The check is green while part of the service is down; alert only after hours of impact |
| `inadequate-control-algorithm` | Retries with no back-off; restarts more than one node at once |
| `actuator-failure` | The command ran but did not take effect; the unit was masked |
| `conflicting-controllers` | GitOps self-heal reverts a hand fix; two watchdogs act on one trigger |
| `process-disturbance` | Backup freeze, I/O storm, power event outside any controller's model |
| `human-factors` | A step is easy to skip at 3 a.m.; two similar commands, one destructive |

A scenario is `controlled` when a control that works today handles it: name the control and cite evidence that it works (a check that fired, a guard that held). Otherwise it is `uncontrolled`, and it is a finding.

## A worked example (generic)

- L-1 Application data is lost. H-1 A block volume is mounted read-write on two nodes at once (L-1).
- C-1 Operator (human). C-2 Stale-mount handler (automated). P-1 Block volume.
- CA-1 C-2 → P-1 "Unmount the stale bind mount". F-1 P-1 → C-2 "Mount-count check" (present). F-2 C-2 → C-1 "Handler result" (missing).
- UCA-1 (`provided-unsafe`) C-2 unmounts while a terminating pod on the same node still writes (H-1). Evidence: a PIR.
- LS-1 (UCA-1): the handler's guard reads the host mount namespace, but the kubelet runs in its own, so it sees no writer (`flawed-process-model`). Uncontrolled → finding: "make the guard read the kubelet's mount namespace".
- LS-2 (UCA-1): the operator never learns the handler acted (`missing-feedback`, F-2). Uncontrolled → finding: "report every handler run in the check output".

## What STPA is not, here

- Not a PIR. A PIR explains one incident; STPA explains how the design lets a class of incidents happen.
- Not a full hazard log. It is targeted: one service, its controllers, the gaps that matter. The `trends.py` backlog says when to look again.
