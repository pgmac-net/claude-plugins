# sre-harness 0.23.0: mutation detection per simple command, approvals paired on words (claude-plugins#32)

## Problem

On one real incident log, `audit.py verify` reported 22 unauthorised mutations against 10 recorded approvals when the ticket was filed. By 0.22.0 that had grown to 33. A reviewer could not find the real entries among them. Causes:

- A multi-line command was classified as one command.
- Patterns matched anywhere in the text: `oom-kill` in a branch name, `mount` in `--text` prose, `patch` inside an approval's `--command`.
- Any `$(...)` made the whole text fall back to substring matching.
- Heredoc bodies (PR bodies, commit messages) were read as commands.
- Read-only forms were flagged: `dd ... count=0`, a bare `mount` listing, `docker exec <c> <read-only command>`.
- An approval paired only with an identical string. So a quoting difference (`-p {"a":1}` vs `-p '{"a":1}'`) or a compound command broke the pair. An approval recorded earlier in the same Bash call never counted.
- `C1='kubectl ... patch ...'` assignments counted as mutations.

## Change

- New `scripts/shell_mutations.py`. It first takes out comments, heredoc bodies and substitutions, and turns newlines into separators. It then splits the line into simple commands and judges each by its program, after assignments, keywords and wrappers are removed. Verbs match as whole words.
- Carriers are opened and what they run is judged: ssh's remote command, `sh/bash -c`, `su -c`, `eval`, `watch`, `xargs`, `find -exec`, `docker exec`, `kubectl exec`, and `sudo -s` fed a heredoc.
- Any other argument holding whitespace or shell syntax is parsed again at command position. Prose such as `--text "person restarted NFS ..."` passes; `--do 'rm -rf x'` does not.
- Variables set earlier in the line are expanded where they are run (`C1=...; $C1`). An unknown `$VAR` as a program fails closed.
- `dd` needs `of=`. A bare `mount` listing is read-only. `docker exec` is a carrier, no longer a verb.
- Pairing in `audit.unauthorised_mutations` is per mutating simple command, on its words:
  - The executed side is the shell's argv. An approval matches through its shell words, its whitespace-split words, or the `argv` stored on new records.
  - Words must be identical, so extra flags stay unpaired.
  - Within one call, an `audit.py record --kind approval` segment takes the approval logged in that call's attempt→completion window, and credits only commands after it. An unused approval carries forward.
- `audit.py record --kind approval` stores `argv` and prints `approves: <canonical form>`.
- Findings gain `segments`, which lists the unpaired simple commands of each call.
- Fails closed as before: over 20,000 characters, or unparsable, means the whole-text patterns.

## Decisions (grilled on the ticket)

1. MUTATING scope unchanged: local changes (`git push`, `sed -i`, `mv`) stay listed.
2. Pairing on argv, approval read two ways.
3. Same-call approvals count only before the command.
4. Strict words: no allow-list of harmless flags.
5. Whole-word verbs, carriers opened.
6. The dd, mount and docker exec narrowings.
7. Harness exemption stays absolute-path-only.
8. Synthetic tests only, because the repo is public. The real log was replayed locally.

Recorded in ADR 0025 and in the CONTEXT.md terms **Simple command**, **Carrier** and **Unauthorised mutation**.

## Deviation from the plan

Decision 5 said the arguments of non-carrier programs would not be searched. Existing tests deliberately require that an unknown program's `'rm -rf x'` argument counts, because the program may run it. So those arguments are parsed again, with matching anchored at command position. Self-review also added `$VAR` expansion in program position. Without it, `C1='...'; $C1` would have been missed, where the old whole-text match caught it.

## Verification

- `pytest`: 880 passed, including 70 new cases in `tests/test_shell_mutations.py` and a `record` argv test.
- Clean: `ruff check`, `ruff format --check`, `pylint` (exit 0), `check_identifiers.py`, `validate_site.py`. Gateway tests: 238 passed.
- Local replay of the incident log: 33 → 8. All kubectl patches, the rollout restart and the restore pair with their approvals. The 8 remaining:
  - a test NFS mount on a node
  - `delete pod` with extra flags
  - `rm` of a temp file on a host
  - `sed -i` of a local file
  - `mv`/`rmdir` in a worktree
  - `git push`
  - a record with an unbalanced quote (its whole-text fallback is correct)
  - a relative-path `autonomy.py match --command '<rollout restart>'`, flagged because decision 7 keeps the exemption absolute-only

## Rollout

Plugin only. No SLO rule change, so sites need no regeneration. Bumping a site's `SRE_HARNESS_REF` pin is optional.
