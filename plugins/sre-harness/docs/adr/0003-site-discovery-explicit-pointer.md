# The site is found by an explicit pointer, never by searching

A skill finds the site repository from, in order: a path the user gave, the `SRE_HARNESS_SITE` environment variable, then `~/.config/sre-harness/site`. If none is set it stops with an error. We rejected looking upward from the working directory, because incidents are started from anywhere and a wrong directory would fail silently or load the wrong site. We rejected an agent-specific plugin setting because other agents and the runner could not read it.

A harness that can approve restarts must not guess which site it is operating on.
