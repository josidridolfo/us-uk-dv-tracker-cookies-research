#!/usr/bin/env bash
# Exercise the pre-crawl exit gate against a live relay, case by case.
#
# The gate's unit tests stub the network, so nothing in them proves the gate
# reaches a real relay, fails closed on a real misconfiguration, or is
# actually in the egress path rather than bypassed. This runs the cases that
# need a live relay and a live container, and reports each verdict.
#
# The gate's contract: 0 on pass, 1 on a country or datacenter mismatch, 2 on
# a relay configuration or read error. Cases 4 and 5 both exit 2, so each
# also asserts on the message that distinguishes them.
#
# Requires: docker, openssl, a repo-root .env carrying IPROYAL_USERNAME and
# IPROYAL_PASSWORD, and the dv-openwpm-base image built by fetch-openwpm.sh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

COMPOSE=(docker compose -f "${REPO_ROOT}/compose.yaml")
GATE=(python -m dv_tracking.egress)

pass_count=0
fail_count=0

report() {
    local name="$1" want_status="$2" got_status="$3" want_regex="$4" output="$5"
    local last
    last="$(echo "${output}" | tail -1)"
    if [ "${want_status}" != "${got_status}" ]; then
        fail_count=$((fail_count + 1))
        printf 'FAIL  %-30s exit %s, wanted %s: %s\n' \
            "${name}" "${got_status}" "${want_status}" "${last}"
        return
    fi
    if [ -n "${want_regex}" ] && ! echo "${output}" | grep -qE "${want_regex}"; then
        fail_count=$((fail_count + 1))
        printf 'FAIL  %-30s exit %s but no match for %q in output: %s\n' \
            "${name}" "${got_status}" "${want_regex}" "${last}"
        return
    fi
    pass_count=$((pass_count + 1))
    printf 'PASS  %-30s exit %s: %s\n' "${name}" "${got_status}" "${last}"
}

run_gate() {
    local geolocation="$1"
    shift
    "${COMPOSE[@]}" run --rm "$@" crawl "${GATE[@]}" "${geolocation}" 2>&1
}

start_relay() {
    IPROYAL_COUNTRY="$1" IPROYAL_SESSION="$(openssl rand -hex 8)" \
        "${COMPOSE[@]}" up -d relay >/dev/null 2>&1
}

# A relay already running belongs to an operator's own work; recreating it
# with this script's country/session would disrupt it, so this refuses
# rather than hijacking it. Only what this script itself starts is torn down.
if [ -n "$("${COMPOSE[@]}" ps --status running -q relay 2>/dev/null)" ]; then
    echo "ERROR: the relay is already running; stop it first, this script manages its own lifecycle" >&2
    exit 1
fi

cleanup() {
    "${COMPOSE[@]}" stop relay >/dev/null 2>&1
    "${COMPOSE[@]}" rm -f relay >/dev/null 2>&1
}
trap cleanup EXIT

# The crawl image copies src at build time, so a stale image would report the
# gate as broken when only the image is out of date.
echo "building the crawl image so the gate under test is the current source"
if ! build_output="$("${COMPOSE[@]}" build crawl 2>&1)"; then
    echo "ERROR: crawl image build failed:" >&2
    echo "${build_output}" >&2
    exit 1
fi

echo "case 1: relay on gb, gate asked for uk -- expect a residential GB exit"
start_relay gb
case1_output="$(run_gate uk)"; status=$?
report "gb relay, uk requested" 0 "${status}" '^PASS:' "${case1_output}"

echo "case 2: relay on gb, gate asked for us -- expect a country mismatch"
output="$(run_gate us)"; status=$?
report "gb relay, us requested" 1 "${status}" 'FAIL: us exit resolved' "${output}"

echo "case 3: relay bypass check -- the gate's exit must differ from the container's own"
gate_ip="$(echo "${case1_output}" | grep -oE 'resolved to [0-9.]+' | awk '{print $3}')"
direct_output="$("${COMPOSE[@]}" run --rm crawl python -c \
    "import urllib.request, json; print(json.load(urllib.request.urlopen('https://ipinfo.io/json'))['ip'])" 2>&1)"
direct_ip="$(echo "${direct_output}" | tail -1)"
if [ -z "${gate_ip}" ] || [ -z "${direct_ip}" ] || [ "${direct_ip}" = "${gate_ip}" ]; then
    fail_count=$((fail_count + 1))
    printf 'FAIL  %-30s gate=%s direct=%s (relay bypassed or unreadable)\n' \
        "relay bypass check" "${gate_ip:-<none>}" "${direct_ip:-<none>}"
else
    pass_count=$((pass_count + 1))
    printf 'PASS  %-30s gate=%s direct=%s\n' "relay bypass check" "${gate_ip}" "${direct_ip}"
fi

echo "case 4: DV_RELAY_PORT malformed, relay still up -- expect a refusal before any socket"
output="$(run_gate uk -e DV_RELAY_PORT=not-a-port)"; status=$?
report "malformed relay port" 2 "${status}" 'DV_RELAY_PORT' "${output}"

echo "case 5: relay stopped -- expect a closed gate after the bounded retry"
"${COMPOSE[@]}" stop relay >/dev/null 2>&1
output="$(run_gate uk)"; status=$?
report "relay unreachable" 2 "${status}" 'after 3 attempts' "${output}"

printf '\n%d passed, %d failed\n' "${pass_count}" "${fail_count}"
[ "${fail_count}" -eq 0 ]
