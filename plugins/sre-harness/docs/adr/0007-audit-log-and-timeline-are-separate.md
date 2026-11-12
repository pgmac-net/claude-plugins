# The audit log and the timeline are separate records

An incident produces two records. The audit log is captured by a harness hook, not written by the model: every command, tool call, result status, approval and denial, as append-only JSON lines. The timeline is curated and human-readable: typed entries on the incident issue. We rejected a single record written by the model, because a model can omit or tidy what it did, and a complete record is too noisy for people to follow during an incident.

## Consequences

- The audit log must be stored in a private repository. It contains commands, hostnames and paths even after secrets are redacted. Site configuration validation refuses a public location.
- Audit events may be forwarded to a log system as a secondary copy. The file stays primary, because the log system may be the thing that is down.
