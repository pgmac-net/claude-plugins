# sre-harness 0.18.1: refuse the alert-source login over plain http off-host

Ticket: pgmac-net/homelabia#262 · PR: pgmac-net/claude-plugins#55 · Branch: `262-refuse-cleartext-login`

## Problem

The unattended-triage runner polls Nagios `statusjson.cgi` with HTTP basic auth. `alerts.fetch` only checked that the URL was http or https, so a site could configure a cleartext login across the network. On macro this was safe (`status_url` is `127.0.0.1`, sre-catalog#44), but nothing enforced it. An automated security review raised it during homelabia#241.

## What changed

- `scripts/alerts.py`: `is_loopback_url` and `cleartext_login_problem`. `fetch()` raises `SiteError` when credentials are set, the scheme is `http`, and the host is not loopback. The message says to use https or a loopback host.
- `scripts/validate_site.py`: `check_runner` reports the same as an error (`runner/status_url`) when `user_env`/`password_env` are set. It checks `runner.status_url`, or the URL derived from the monitoring source's endpoint.
- The gateway's Nagios feed calls `alerts.fetch`, so it is covered with no gateway code change.
- Docs: gateway README ("Unattended triage") and the `site.yaml` template comment.
- Versions: plugin 0.18.1, marketplace 1.29.1, gateway 0.13.1.

## Decisions (from the grilling session)

1. **Only when credentials are set.** Without a login there is no secret to leak, and an anonymous http source is valid. `fetch` and the validator apply the same rule, so they cannot disagree.
2. **Loopback by literal host, no DNS.** `localhost` (case-insensitive, trailing dot ignored) or `ipaddress.ip_address(host).is_loopback` (127.0.0.0/8, `::1`, bracketed form). `localhost.localdomain` and `/etc/hosts` aliases count as off-host. A DNS answer can change after validation, so name resolution would let the validator and `fetch` disagree on the same string.
3. **Ship as 0.18.1** with an ansible deploy PR. macro is unaffected (`127.0.0.1`); the bump keeps it on current code.

## Tests

`tests/test_runner.py`: `TestCleartextLogin` (fetch) and new cases in `TestValidate`. They cover refuse (http, off-host, login), allow (127.x, `[::1]`, `localhost`, `LOCALHOST.`), non-loopback names and addresses refused (`localhost.localdomain`, `127.0.0.1.example.com`, a public IPv4, `[::2]`), allow (no login), allow (https), plus the validator for an explicit URL, an endpoint-derived URL, loopback and no login.

Verified locally: 779 plugin tests, 147 gateway tests, ruff, ruff format, pylint (exit 0), template validation, identifier scan.

## Deviations from the plan

- **The ansible deploy PR is not raised yet.** `vars/sre_gateway.yml` pins a commit SHA of claude-plugins, not a version. The 0.18.1 commit exists only on the PR branch, and a squash merge changes the SHA, so pinning the branch commit could break the deploy. After claude-plugins#55 merges, set `sre_gateway_plugin_version` to the merge commit and raise the PR (currently `98d6f8af76e7eb34e314c6519430f500be4a8ec7`, 0.18.0).
- The test for the non-loopback IPv4 uses `203.0.113.5` (TEST-NET-3), because the identifier scan rejects private addresses in the plugin.

## Out of scope

The Nagios MCP sends `nagiosadmin` over `http://status.int.pgmac.net:8080` from cluster pods (pgk8s#1135).
