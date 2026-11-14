# sre-harness advisory helper (M2, part 4 of 4)

Ticket: pgmac-net/homelabia#236.

## What was built

`sre-harness` v0.6.0 adds an optional advisory helper, `scripts/assist.py`,
backed by a fast judgment model.

| Command | Question | Answer |
|---|---|---|
| `severity` | Which severity does this impact suggest? | A level with probabilities. Uses the site's own criteria document when its checkout is present |
| `runbooks` | Which runbooks cover these symptoms? | A ranking by file name, plus the probability that none does |
| `entry-type` | What type is this timeline entry? | One of the seven types |

## What it never does

Approve, block or select an action. Decide whether a command is Tier 2. Set a
final severity. See ADR 0012.

## Safety

- **Off by default.** The template ships it disabled. A site opts in.
- **Cut, redact, mask, in that order.** The text is cut at a word boundary to
  a window that is scanned in full; secrets are redacted while the text is
  still as written; then site identifiers from the site's mask file and
  private addresses become stable placeholders. Runbook
  file names are never sent: options are numbered. If a mask file is
  configured and missing, nothing is sent.
- **Only our labels come back.** An answer that is not one of the options
  offered is refused; probabilities for unknown labels are dropped.
- **Never blocks.** No key, a timeout or an error gives
  `{"available": false, "reason": …}` and exit 0.
- **On the record.** Each call during an incident is written to the audit log.

## Found in review

An automated security review of the first push found two ways masking could be bypassed. Both were valid.

| Finding | Change |
|---|---|
| An identifier joined to other text by `-` or `_` was not masked; IPv6 private and CGNAT addresses were not covered | An identifier is masked wherever it is not part of a longer run of letters and digits; unique-local and link-local IPv6, CGNAT and IPv4 link-local are masked |
| Text was cut and redacted before masking, and a long string had a hash of its unmasked original appended | Nothing derived from the original is added. See the order below |
| (second review) Masking before redaction could change part of a secret and stop its pattern matching; a secret straddling the scan boundary could be partly sent | The order is now: cut first, back to the last whitespace so no token is split; redact everything that remains; mask last. Every character that can be sent has been scanned |
| (third review) With no whitespace near the cut, the fallback still cut inside an unbroken run | The cut is always at whitespace of any kind; text with none to cut at is replaced by a placeholder and not sent |

A later review, of the site change that enabled the helper, found a gap in the order:
a site may list an address prefix (for partial forms such as "the 10.1.2 network"), and if
identifiers were masked first the prefix split a whole address and left its last octet.
Masking addresses first, in turn, broke an identifier that itself contains an address
(it was split, and its other half sent). Identifiers and addresses are now found together in
the original text and replaced in one pass: where two overlap, the one that starts first
wins, and of two that start together the longer wins. A full address beats a listed prefix
of it, and an identifier containing an address is masked whole.

## Verification

- 328 tests pass; ruff clean; pylint exit 0. The service call is replaced in
  tests; nothing leaves the machine.
- Live, with generic made-up text: an outage for all customers gave P1 (1.0);
  one slow dashboard for one user gave P4 (0.92); "deleted the stuck pod …
  errors stopped" was typed `action` (1.0).
- Live runbook ranking over twenty real runbooks with masking on: a stale
  volume attachment description ranked the matching runbook first (1.0); an
  irrelevant symptom gave "none" (1.0).
- A bad key and a missing runbooks directory each reported unavailable and
  exited 0.

## Limits

- Its answers can differ between identical calls. That is why it only advises.
- Masking is by listed identifiers and address patterns. A name missing from
  the mask file is sent as written.
- Incident text and runbook prose can steer its answers. Nothing is gated on them.
