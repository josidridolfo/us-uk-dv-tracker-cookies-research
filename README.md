# us-uk-dv-tracker-cookies-research

Work in progress.

A sector-specific web-measurement study of pre-consent third-party tracking on
United Kingdom and United States domestic violence support websites. The study
visits each site with a fresh browser, records the network requests, cookies,
and storage set before any consent interaction, and compares the two
jurisdictions at an aggregate level.

## The relay and the exit gate

The residential-exit credentials belong to a relay sidecar, not to this
codebase: it terminates an unauthenticated listener and attaches the upstream
username, password and routing parameters itself, so no credential crosses into
Python. `IPROYAL_USERNAME` and `IPROYAL_PASSWORD` belong in the git-excluded
`.env` at the repo root that the relay container reads.

`IPROYAL_COUNTRY` and `IPROYAL_SESSION` are deliberately not kept in that file.
Country follows the jurisdiction under crawl and the session token is generated
per run, so both are supplied at invocation; `IPROYAL_LIFETIME` defaults to
`10m`. The relay refuses to start if any of the four is absent.

The pre-crawl exit gate, invoked as `python -m dv_tracking.egress
<geolocation>` inside the crawl container, reads `DV_RELAY_HOST` and
`DV_RELAY_PORT` to find that listener. Both are required with no default, so an
unset relay address fails the gate closed rather than resolving to some other
listener on the machine. The gate runs inside the crawl container, which is
where the relay's listener resolves and where the browser's own egress will be
observed; the project is not pip-installed into that image, so no console
script exists there to run instead.

The old raw write path (`RawStore`, a trial's `metadata.json` and its
`exit_info` object) has no producer any more; the OpenWPM crawl entrypoint
below writes a different cell layout instead.

## The crawl entrypoint

Invoked inside the crawl container, not as a console script (the image never
pip-installs the project, so no entry point would resolve):

```bash
python -m dv_tracking.crawl.openwpm_run uk --sites /data/corpus/uk_selected_250.csv
```

**This currently refuses to crawl.** Firefox's proxy and tracking-protection
prefs are not pinned until the next PR, so before building any
`CommandSequence` the runner asserts `BrowserParams` already pins a manual
proxy at the verified relay's host and port, pins `privacy.trackingprotection.*`
off, and pins `tp_cookies` so no third-party cookie gets blocked or
partitioned -- exiting 3 if any of it is missing. That assertion proves
configuration, not behaviour: prefs are reapplied on every browser relaunch,
so it means something continuously once pinned, but WebRTC and DNS-over-HTTPS
bypass `network.proxy.*` entirely, and nothing today reads the exit IP from
inside a page load the way the gate itself does from outside. Proving the
browser's own traffic took this path is a later PR.

Exit codes are the `ExitCode` enum in `openwpm_run.py`: 0 completed, 1 no
sites matched, 2 the relay/exit gate did not pass or raised, 3 prefs not
pinned, 4 the crawl failure budget was exceeded, 5 another invocation holds
the cell lock, 6 `--exit-recheck-seconds` leaves too little margin before
`--iproyal-lifetime`, 7 an active condition has no command yet, 8 any other
unanticipated failure -- a distinct code so it is never mistaken for "no
sites matched" (1) and read as not worth retrying.

`--iproyal-lifetime` (default from the `IPROYAL_LIFETIME` env var, `10m`)
and `--exit-recheck-seconds` (default `600`) share the same duration format
("10m", "600s", or a bare integer of seconds) and are compared directly: the
recheck cadence must leave at least 30 seconds of margin before the relay
session could rotate, so the out-of-the-box defaults -- both 600 seconds --
now refuse (exit 6) rather than sitting exactly on that boundary. Pass a
shorter `--exit-recheck-seconds` or a longer `--iproyal-lifetime` explicitly.

One cell is `data/raw/<crawl_date>_<geolocation>/openwpm/`, holding:

- `crawl-data.sqlite` -- OpenWPM's own primary artifact and the resume ledger.
- `manifest.json` -- written once per cell (`ordering_seed`, `instrument`,
  `schema_version`, each pinned: a later invocation whose own values differ
  raises rather than proceeding against the wrong cell). Reopened to record
  `refused`/`refusal_reason` when a run refuses, and cleared again the next
  time an invocation gets past the check that refused it -- `refused: true`
  describes the last invocation's own gate result, not the cell permanently.
- `runs.jsonl` -- two lines per invocation, joined by `run_id`: a start row
  (repo commit, image and Firefox versions, `tp_cookies`, which instruments
  were enabled, the active conditions and their dwells, the finalize drain
  tail, the sites file and its hash, and other facts that can legitimately
  differ between a cell's invocations) and an end row (`ended_at_utc`, the
  exit code, a reason, and how many visits were left). Every exit from the
  crawl itself writes the end row, including an unanticipated exception, so
  a cell that stopped partway is never silent about why; the one case with
  no row at all is losing the cell lock before a `run_id` exists yet.
- `exit-checks.jsonl` -- one line per exit-gate check taken, pass or fail,
  carrying `run_id` and its own `checked_at_utc`.
- `visits.jsonl` -- one line per attempted visit, mapping OpenWPM's `visit_id`
  to `(site_id, condition, replicate)`, plus the `run_id` and `checked_at_utc`
  of the exit check governing it and its `work_list_position`. Written
  *before* the page load, not after: a failed get would otherwise leave no
  identity for that visit_id, and resume would re-crawl it on every future
  invocation forever.
- `boundaries.jsonl` -- one line per visit recording the synthetic
  phase-boundary instant the `no_interaction` arm logs in place of a click,
  and `offset_from_get_start_s`: `GetCommand`'s own alert probe adds 0.5 to
  1.5 seconds between the pre-dwell ending and this boundary running, site-
  dependently, so the matched-offset comparison the design calls for needs
  this recorded rather than assumed fixed.

Resume reconciles `crawl_history` (which outcome) against `visits.jsonl`
(which site) on every invocation; nothing else is kept as a ledger. A visit
is terminal when its last `crawl_history` row is the finalize command with
status `ok`, or when its last row has any other status -- those are the only
two ways OpenWPM's BrowserManager ends a live sequence. A visit whose last
row is `ok` but is not the finalize command (e.g. a kill mid-dwell, after
only Initialize and Get) is not terminal and gets a fresh `visit_id` on the
next invocation. A terminal visit's outcome is `"completed"` for the clean
case, otherwise the literal status (`"timeout"`, `"critical"`, `"error"`, or
`"neterror"`, never collapsed into one another) plus whether OpenWPM's own
storage marked it `incomplete_visits` -- independent of outcome, since a
timed-out finalize is still terminal but still incomplete. *Any* terminal
outcome counts as done and is not retried; a recorded failure is a result,
not grounds for a silent retry. A kill between the identity stamp and the
finalize command leaves one `visit_id` stamped with no outcome and a second
`visit_id` for the same `(site, condition, replicate)` once it is re-crawled;
the higher `visit_id`, the one with a terminal `crawl_history` row, is
authoritative, and the first is an orphaned, harmless row in `visits.jsonl`.

For five seconds after `FinalizeCommand` tears down the tab, records can
still arrive and get attributed to that `visit_id` with timestamps past
`boundary_utc + post_dwell_s` (`runs.jsonl`'s `finalize_drain_tail_s`); an
analysis that truncates strictly at that boundary will silently drop them.

## Crawl container

The crawl stage runs in a container; the analysis stage runs on the host. The
browser is the unbranded Firefox that OpenWPM pins, which is published for
`linux/amd64` only, so reported crawls run on an amd64 host. Emulated runs on
another architecture distort the fixed dwell windows and are suitable for
development only.

Two build steps, because upstream's Dockerfile takes its own source tree as the
build context:

```bash
./scripts/fetch-openwpm.sh      # pinned clone -> dv-openwpm-base
./scripts/build-crawl-image.sh  # refuses on a dirty tree, then builds the crawl image
```

Both commands run from the repo root. `compose.yaml` lives there rather than
beside the Dockerfile because Compose discovers `.env` in the directory of the
first compose file, and the residential-exit credentials live at the root.

`fetch-openwpm.sh` clones OpenWPM at the pinned tag, refuses to build if the
tag no longer resolves to the pinned commit, rewrites upstream's floating base
image to a pinned digest, and records the result in
`data/openwpm-base-provenance.txt`.

The `relay` service exists because Firefox cannot authenticate to an upstream
proxy without a prompt. It terminates an unauthenticated listener on the
compose network and forwards upstream with the residential-exit credentials
attached, so the browser is configured with `relay:8080` and no credential.

Starting the relay for one jurisdiction:

```bash
IPROYAL_COUNTRY=gb IPROYAL_SESSION=$(openssl rand -hex 8) \
    docker compose up -d relay
```

Verifying the stack:

```bash
docker compose run --rm crawl       # imports the crawl entrypoint and its full dependency chain
./scripts/check-exit-gate.sh        # builds, starts a relay, and exercises the gate
```

`check-exit-gate.sh` builds the current image, starts the relay for a
jurisdiction, and runs the gate through it end to end: a pass, a country
mismatch, a malformed relay address, an unreachable relay, and a check that
the gate's reported exit actually differs from the container's own unproxied
egress -- a relay silently out of the path would otherwise pass case one for
free on a UK residential connection. It manages the relay's own lifecycle and
refuses to run against one already started for other work. The pre-crawl gate
enforces that the exit reads residential and in-country before any site is
visited.

## Cookie essentiality

A cookie is treated as essential when it is both strictly necessary and
short-lived. Strict necessity is taken from the category assigned by the pinned
Open Cookie Database. Short-lived means the cookie is either a session cookie or
has an observed expiry within 30 days of the crawl instant. The observed
lifetime is used -- the `expires_unix` value captured during the crawl, or the
session flag -- not any declared retention period. Everything else is
non-essential: analytics and marketing cookies, cookies whose name is not
matched by the database, and strictly-necessary cookies whose observed lifetime
runs beyond the window. The reference instant is the crawl start recorded per
trial, so the classification is stable across re-analysis.

## Sites & Results
Available upon request to verified researchers. Message me or email me with the subject [UK-US DV Data Request]: josid@ridol.fo
