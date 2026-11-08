#!/usr/bin/env bash
# ==============================================================================
# nagios-graph.sh — signed nagiosgraph URLs for incident evidence
#
# Builds the same HMAC-signed, expiring /api/graph URL that notify_slack.sh
# (pgmac-net/nagios-config) embeds in Slack alerts, proxied through the public
# status page (pgmac-net/nagios-public-status-page, docs/SLACK_GRAPH_PROXY.md):
#
#   sig = HMAC-SHA256(GRAPH_SIGNING_SECRET, "host|service|period|timet|expires")
#
# The secret and base URL are read from slack.ini inside the nagios4 container
# on macro over SSH, held in memory only, and never printed.
#
# USAGE:
#   nagios-graph.sh [-p period] [-t when] [-T ttl] [-o out.png] <host> <service>
#   nagios-graph.sh -L <host>          # list graphable services (have perfdata)
#
#   -p period  day|week|month|quarter|year (default: day)
#   -t when    window END: unix epoch, anything `date -d` parses
#              ("2026-11-08 15:30", "2 hours ago"), or "live". Default: now.
#              "live" (timet=0) drifts with every fetch — never use it in a
#              record that someone reads later.
#   -T ttl     signature lifetime in seconds (default: 2592000 = 30 days)
#   -o file    also download the PNG to file and validate it
#
# Prints the signed URL on stdout.
#
# EXIT: 0 ok · 1 usage/config error · 2 graph empty ("No data available": the
#       proxy returns HTTP 200 with a 608x23 PNG for unknown host/service or no
#       RRD data, so a status-code check alone is not enough) · 3 fetch failed
#
# ENV: NAGIOS_SSH_HOST (default macro), NAGIOS_CONTAINER (default nagios4),
#      GRAPH_SIGNING_SECRET + STATUSPAGE_BASE_URL (skip the SSH lookup)
# ==============================================================================
set -euo pipefail

SSH_HOST="${NAGIOS_SSH_HOST:-macro}"
CONTAINER="${NAGIOS_CONTAINER:-nagios4}"
SLACK_INI=/opt/nagios/etc/objects/pgmac.net/slack.ini
STATUS_DAT=/opt/nagios/var/status.dat

usage() {
    sed -n '/^# USAGE:/,/^# ENV:/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 1
}

list_services() {
    # Services with non-empty performance_data are the ones nagiosgraph can draw.
    # shellcheck disable=SC2029  # host is expanded locally on purpose
    ssh "${SSH_HOST}" "docker exec ${CONTAINER} awk -v want='$1' '
        /^servicestatus \\{/ { s=1; h=\"\"; d=\"\"; p=0 }
        s && /^\\thost_name=/           { sub(/^[^=]*=/, \"\"); h=\$0 }
        s && /^\\tservice_description=/ { sub(/^[^=]*=/, \"\"); d=\$0 }
        s && /^\\tperformance_data=./   { p=1 }
        s && /^\\t\\}/ { if (h == want && p) print d; s=0 }
    ' ${STATUS_DAT}"
}

PERIOD=day
WHEN=now
TTL=2592000
OUT=""
LIST=""
while getopts ":p:t:T:o:L:h" opt; do
    case "${opt}" in
        p) PERIOD="${OPTARG}" ;;
        t) WHEN="${OPTARG}" ;;
        T) TTL="${OPTARG}" ;;
        o) OUT="${OPTARG}" ;;
        L) LIST="${OPTARG}" ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))

if [ -n "${LIST}" ]; then
    list_services "${LIST}"
    exit 0
fi

[ $# -eq 2 ] || usage
HOST="$1"
SERVICE="$2"

case "${PERIOD}" in
    day|week|month|quarter|year) ;;
    *) echo "ERROR: period must be day|week|month|quarter|year" >&2; exit 1 ;;
esac

NOW=$(date +%s)
case "${WHEN}" in
    live) TIMET=0 ;;
    now)  TIMET="${NOW}" ;;
    *[!0-9]*) TIMET=$(date -d "${WHEN}" +%s) || { echo "ERROR: cannot parse -t '${WHEN}'" >&2; exit 1; } ;;
    *) TIMET="${WHEN}" ;;
esac
# The proxy forwards offset = now - timet; a future anchor would be clamped to
# live anyway, so pin it to now and keep the record stable.
[ "${TIMET}" -le "${NOW}" ] || TIMET="${NOW}"

if [ -z "${GRAPH_SIGNING_SECRET:-}" ] || [ -z "${STATUSPAGE_BASE_URL:-}" ]; then
    # shellcheck disable=SC2029  # expanded locally on purpose
    CONF=$(ssh "${SSH_HOST}" "docker exec ${CONTAINER} grep -E '^(GRAPH_SIGNING_SECRET|STATUSPAGE_BASE_URL)=' ${SLACK_INI}") \
        || { echo "ERROR: cannot read ${SLACK_INI} via ssh ${SSH_HOST}" >&2; exit 1; }
    GRAPH_SIGNING_SECRET=$(printf '%s\n' "${CONF}" | sed -n 's/^GRAPH_SIGNING_SECRET=//p' | tr -d "\"'")
    STATUSPAGE_BASE_URL=$(printf '%s\n' "${CONF}" | sed -n 's/^STATUSPAGE_BASE_URL=//p' | tr -d "\"'")
fi
[ -n "${GRAPH_SIGNING_SECRET}" ] && [ -n "${STATUSPAGE_BASE_URL}" ] \
    || { echo "ERROR: GRAPH_SIGNING_SECRET / STATUSPAGE_BASE_URL not found" >&2; exit 1; }

EXPIRES=$(( NOW + TTL ))
SIG=$(printf '%s' "${HOST}|${SERVICE}|${PERIOD}|${TIMET}|${EXPIRES}" \
    | openssl dgst -sha256 -hmac "${GRAPH_SIGNING_SECRET}" | awk '{print $NF}')
HOST_ENC=$(jq -rn --arg s "${HOST}" '$s|@uri')
SERVICE_ENC=$(jq -rn --arg s "${SERVICE}" '$s|@uri')
URL="${STATUSPAGE_BASE_URL%/}/api/graph?host=${HOST_ENC}&service=${SERVICE_ENC}&period=${PERIOD}&timet=${TIMET}&expires=${EXPIRES}&sig=${SIG}"

echo "${URL}"

if [ -n "${OUT}" ]; then
    CODE=$(curl -s -o "${OUT}" -w '%{http_code}' --max-time 20 "${URL}") || CODE=000
    if [ "${CODE}" != "200" ]; then
        echo "ERROR: graph fetch returned HTTP ${CODE}" >&2
        rm -f "${OUT}"
        exit 3
    fi
    # PNG height lives in IHDR bytes 20-23. Real graphs are ~225px tall; the
    # "No data available" placeholder is 23px.
    HEIGHT=$(od -An -tu1 -j20 -N4 "${OUT}" | awk '{print $1*16777216 + $2*65536 + $3*256 + $4}')
    if [ "${HEIGHT:-0}" -lt 50 ]; then
        echo "ERROR: no graph data for ${HOST}/${SERVICE} (try -L ${HOST} for valid service names)" >&2
        rm -f "${OUT}"
        exit 2
    fi
    echo "saved ${OUT}" >&2
fi
