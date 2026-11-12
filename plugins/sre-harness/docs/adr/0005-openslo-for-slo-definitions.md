# SLOs are defined in OpenSLO, stored beside the catalog

SLO definitions are OpenSLO YAML files in the site repository, next to the catalog entity they belong to. The metrics backend only evaluates them: its recording and alert rules are generated from these files. We chose a vendor-neutral format because the portable core must not assume one metrics vendor, and because definitions kept only in a vendor's UI cannot be reviewed, versioned or moved.
