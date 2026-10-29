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

**This currently refuses to crawl.** Firefox's proxy prefs are not pinned
until the next PR, so before building any `CommandSequence` the runner
asserts `BrowserParams.prefs` already pins a manual proxy at the verified
relay's host and port, and exits 3 if it does not. That assertion proves
configuration, not behaviour: prefs are reapplied on every browser relaunch,
so it means something continuously once pinned, but WebRTC and DNS-over-HTTPS
bypass `network.proxy.*` entirely, and nothing today reads the exit IP from
inside a page load the way the gate itself does from outside. Proving the
browser's own traffic took this path is a later PR.

One cell is `data/raw/<crawl_date>_<geolocation>/openwpm/`, holding:

- `crawl-data.sqlite` -- OpenWPM's own primary artifact and the resume ledger.
- `manifest.json` -- written once per cell (`ordering_seed`, `instrument`,
  `schema_version`); reopened only if a run refuses, to record `refused` and
  `refusal_reason` without losing the seed.
- `runs.jsonl` -- one line per invocation: the repo commit, image and Firefox
  versions, the active conditions and their dwells, the sites file and its
  hash, and other facts that can legitimately differ between a cell's
  invocations.
- `exit-checks.jsonl` -- one line per exit-gate check taken, pass or fail.
- `visits.jsonl` -- one line per attempted visit, mapping OpenWPM's `visit_id`
  to `(site_id, condition, replicate)`. Written *before* the page load, not
  after: a failed get would otherwise leave no identity for that visit_id, and
  resume would re-crawl it on every future invocation forever.
- `boundaries.jsonl` -- one line per visit recording the synthetic
  phase-boundary instant the `no_interaction` arm logs in place of a click.

Resume reconciles `crawl_history` (which outcome) against `visits.jsonl`
(which site) on every invocation; nothing else is kept as a ledger. A visit
with *any* terminal outcome -- completed, timed out, or errored -- counts as
done and is not retried; a recorded failure is a result, not grounds for a
silent retry. A kill between the identity stamp and the finalize command
leaves one `visit_id` stamped with no outcome and a second `visit_id` for the
same `(site, condition, replicate)` once it is re-crawled; the second one,
the one with a terminal `crawl_history` row, is authoritative, and the first
is an orphaned, harmless row in `visits.jsonl`.

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
docker compose run --rm crawl       # imports OpenWPM
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
