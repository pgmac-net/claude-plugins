# STPA analyses

One file per service, `<service>.md`, written with the `design-review` skill and
checked by `stpa.py check`. The front matter holds the analysis (losses, hazards,
control structure, unsafe control actions, loss scenarios); the body explains it
and carries a diagram that `stpa.py render` regenerates.
