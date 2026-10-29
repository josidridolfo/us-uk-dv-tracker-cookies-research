#!/usr/bin/env bash
# Exercise the pre-crawl exit gate against a live relay, case by case.
#
# The gate's unit tests stub the network, so nothing in them proves the gate
# reaches a real relay or fails closed on a real misconfiguration. This runs
# the cases that need a live exit and reports each verdict.
#
# The gate's contract: 0 on pass, 1 on a country or datacenter mismatch, 2 on a
# relay configuration or read error. Cases 3 and 4 both exit 2, so each also
# asserts on the message that distinguishes them.
#
# Requires: docker, a repo-root .env carrying IPROYAL_USERNAME and
# IPROYAL_PASSWORD, and the dv-openwpm-base image built by fetch-openwpm.sh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

COMPOSE=(docker compose -f "${REPO_ROOT}/compose.yaml")
GATE=(python -m dv_tracking.egress)

pass_count=0
fail_count=0

report() {
    local name="$1" want_status="$2" got_status="$3" want_text="$4" output="$5"
    local last
    last="$(echo "${output}" | tail -1)"
    if [ "${want_status}" != "${got_status}" ]; then
        fail_count=$((fail_count + 1))
        printf 'FAIL  %-30s exit %s, wanted %s: %s\n' \
            "${name}" "${got_status}" "${want_status}" "${last}"
        return
    fi
    if [ -n "${want_text}" ] && ! echo "${output}" | grep -q "${want_text}"; then
        fail_count=$((fail_count + 1))
        printf 'FAIL  %-30s exit %s but no %q in output: %s\n' \
            "${name}" "${got_status}" "${want_text}" "${last}"
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

# The crawl image copies src at build time, so a stale image would report the
# gate as broken when only the image is out of date.
echo "building the crawl image so the gate under test is the current source"
"${COMPOSE[@]}" build crawl >/dev/null 2>&1 || {
    echo "ERROR: crawl image build failed; run compose build directly to see why" >&2
    exit 1
}

echo "case 1: relay on gb, gate asked for uk -- expect a residential GB exit"
start_relay gb
output="$(run_gate uk)"; status=$?
report "gb relay, uk requested" 0 "${status}" "residential" "${output}"

echo "case 2: relay on gb, gate asked for us -- expect a country mismatch"
output="$(run_gate us)"; status=$?
report "gb relay, us requested" 1 "${status}" "Do NOT run the crawl" "${output}"

echo "case 3: relay stopped -- expect a closed gate after the bounded retry"
"${COMPOSE[@]}" stop relay >/dev/null 2>&1
output="$(run_gate uk)"; status=$?
report "relay unreachable" 2 "${status}" "exit lookup" "${output}"

echo "case 4: DV_RELAY_PORT malformed -- expect a refusal before any socket"
output="$(run_gate uk -e DV_RELAY_PORT=not-a-port)"; status=$?
report "malformed relay port" 2 "${status}" "DV_RELAY_PORT" "${output}"

"${COMPOSE[@]}" down >/dev/null 2>&1
printf '\n%d passed, %d failed\n' "${pass_count}" "${fail_count}"
[ "${fail_count}" -eq 0 ]
