# Portable core, site data in a separate repository

The harness was built for one homelab but must be usable elsewhere, so the plugin holds only logic and knows nothing about any site. Every site fact (hosts, endpoints, repositories, channels, people, services) lives in a private site repository that the plugin reads. We chose this over a site-specific plugin, which is faster to write but cannot be shared, and over designing a generic product for unknown users, which adds abstraction with no second user to test it.

## Consequences

- No site identifier may appear in the plugin. `scripts/check_identifiers.py` enforces this in CI with generic patterns, and locally against a site's private denylist.
- Skills use repository aliases and catalog lookups, never hardcoded paths.
