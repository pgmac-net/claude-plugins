# Questions are read-only for life, yield to incidents, and are logged by the gateway

A responder can ask the chat gateway a reliability question with `/sre <question>` without opening an incident (homelabia#254). This records the shape of that mode.

**A question runs under its own policy, derived from the chat one.** The chat allow-list, minus every call that writes a record or a system (`incident.py` open, attach, set, close, promote and dismiss, `timeline.py`, `autonomy.py run`), which are denied outright. Any other call reaches the gateway's permission callback and is refused at once with no approval request, as in an unattended triage (ADR 0018). Deriving it from `rules()` keeps one source of truth; a hand-kept narrower list (only `slo.py`, `kube_read.py` and the monitoring MCP tools) was rejected because it would drift and could not answer questions about past incidents. Pi's strict matcher gets the same derived rules.

**A question never becomes an incident.** If the agent finds a live problem, it says so and suggests `/incident <summary>`; the incident starts fresh. Converting a question in place, as triage-only becomes chat, was rejected: a second one-way transition to build, record and test, for evidence that is cheap to gather again.

**Questions yield to incident work.** A question starts only if it leaves at least one agent session free, and is refused at once otherwise; it never waits in line. An idle question is ended when an incident or triage needs its slot. Each question stops after `question_idle_minutes` (default 5) of silence or 10 turns, and is not kept across a restart, so it has no resume.

**The gateway, not the hook, logs a question.** The audit hook records only for a session bound to an incident, and Pi has no hook. The gateway already sees every tool call and output, so it writes a chained question log per question (asker, scrubbed question, redacted inputs, output size and hash, refusals) under its state directory, and its hourly sweep removes it after 30 days. It is never committed: a question cannot change anything, and a daily audit pull request would be toil for a record of reads. Answers live only in the hub thread; mirroring them to a tracker would put a write path back into a read-only mode.

`/sre` is the only trigger. A hub mention still starts an incident: a question mark is no sign that something is not broken.
