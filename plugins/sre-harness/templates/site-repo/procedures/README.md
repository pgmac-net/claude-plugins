# Site procedures

How things are done at this site, written for the agent working an incident. The harness's skills decide *when* a procedure is read; these files say *how*.

`site.yaml` lists which files belong to which stage under `incident.procedures`:

| Stage | Read when | Typically covers |
|---|---|---|
| `triage` | Establishing the picture | How to query each monitoring source, fallbacks when one is down, known gotchas |
| `evidence` | Diagnosing and verifying | How to capture graphs and logs, where evidence is kept |
| `comms` | Any time people are told something | Channels, who reads them, what is and is not posted |
| `review` | Writing the post-incident review | Document format, where it is published, how action items are filed |

Write them as you would brief a capable colleague on their first on-call shift: exact commands, what the output means, and what has gone wrong before. Scripts a procedure needs go in `procedures/scripts/`.
