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

Before building any `CommandSequence`, the runner pins `BrowserParams.prefs`
from the just-verified relay address and asserts the result: a manual proxy
at the relay's host and port, `privacy.trackingprotection.*` off,
`network.trr.mode = 5` (DNS-over-HTTPS off by choice, not merely by Firefox's
current default) and `media.peerconnection.enabled = False` (WebRTC off), and
`tp_cookies` so no third-party cookie gets blocked or partitioned -- exiting 3
if any of it does not match. That assertion proves configuration, not
behaviour: prefs are reapplied on every browser relaunch, so it means
something continuously once pinned, but nothing today reads the exit IP from
inside a page load the way the gate itself does from outside. Proving the
browser's own traffic took this path is a later PR.

Exit codes are the `ExitCode` enum in `openwpm_run.py`: 0 completed, 1 no
sites matched, 2 the relay/exit gate did not pass or raised, 3 prefs not
pinned, 4 the crawl failure budget was exceeded, 5 another invocation holds
the cell lock, 6 `--exit-recheck-seconds` leaves too little margin before
`--iproyal-lifetime`, 7 an active condition's action is not in the
configured consent interactor's `supported_actions`, 8 any other
unanticipated failure -- a distinct code so it is never mistaken for "no
sites matched" (1) and read as not worth retrying.

`--iproyal-lifetime` (default from the `IPROYAL_LIFETIME` env var, `10m`)
and `--exit-recheck-seconds` (default `300`) share the same duration format
("10m", "600s", or a bare integer of seconds) and are compared directly: the
recheck cadence must leave at least 30 seconds of margin before the relay
session could rotate. The defaults leave that margin out of the box; a
cadence raised to sit within 30 seconds of `--iproyal-lifetime` refuses
(exit 6) rather than being accepted on the boundary.

One cell is `data/raw/<crawl_date>_<geolocation>/openwpm/`, holding:

- `crawl-data.sqlite` -- OpenWPM's own primary artifact and the resume ledger.
- `content-store.leveldb/` -- the unstructured content store, a LevelDB
  database (LevelDB's own format is a directory, not a single file, hence
  the directory living beside `crawl-data.sqlite` rather than as a sibling
  file). Keyed by `content_hash`, which is also the join key into
  `http_responses.content_hash`: a response's bytes live in this store
  under the same hash recorded on its row. The store is content-addressed,
  so a body that recurs verbatim across sites and visits (a CMP vendor's
  script served from the same CDN, for instance) is written once.
- `manifest.json` -- written once per cell (`ordering_seed`, `instrument`,
  `schema_version`, each pinned: a later invocation whose own values differ
  raises rather than proceeding against the wrong cell). Reopened to record
  `refused`/`refusal_reason` when a run refuses, and cleared again the next
  time an invocation gets past the check that refused it -- `refused: true`
  describes the last invocation's own gate result, not the cell permanently.
- `runs.jsonl` -- two lines per invocation, joined by `run_id`: a start row
  (repo commit, image and Firefox versions, `tp_cookies`, the full pinned
  `prefs` dict as `pinned_prefs`, and `pref_fingerprint` -- a SHA-256 over
  the tracking-protection and leak-closure prefs plus `tp_cookies`, so two
  cells can be compared for configuration drift without diffing the dict by
  hand. The proxy target is deliberately left out of the hash: the UK and
  US arms point at different relays by design, so including it would make
  every cell's fingerprint differ even when the protections are identical.
  The start row also carries which instruments were enabled, the active
  conditions and their dwells, the finalize drain tail, the sites file and
  its hash, and other facts that can legitimately differ between a cell's
  invocations) and an end row (`ended_at_utc`, the exit code, a reason, and
  how many visits were left). Every exit from the crawl itself writes the
  end row, including an unanticipated exception, so a cell that stopped
  partway is never silent about why; the one case with no row at all is
  losing the cell lock before a `run_id` exists yet.
- `exit-checks.jsonl` -- one line per exit-gate check taken, pass or fail,
  carrying `run_id` and its own `checked_at_utc`.
- `visits.jsonl` -- one line per attempted visit, mapping OpenWPM's `visit_id`
  to `(site_id, condition, replicate)`, plus the `run_id` and `checked_at_utc`
  of the exit check governing it and its `work_list_position`. Written
  *before* the page load, not after: a failed get would otherwise leave no
  identity for that visit_id, and resume would re-crawl it on every future
  invocation forever.
- `boundaries.jsonl` -- one line per visit recording `boundary_utc`: the
  consent interactor's own reported click instant for a successful
  `reject_all`/`accept_all` decision (`boundary_source: "engine_reported"`),
  or this command's own timestamp otherwise -- `no_interaction`, no banner
  detected, or a failed/timed-out attempt (`boundary_source: "synthetic"`).
  `offset_from_get_start_s` is `boundary_utc` minus the preceding
  `StampVisitCommand`'s timestamp: `GetCommand`'s own alert probe adds 0.5
  to 1.5 seconds between the pre-dwell ending and this command running,
  site-dependently, so the matched-offset comparison the design calls for
  needs this recorded rather than assumed fixed.
- `interactions.jsonl` -- one line per visit from the same command,
  recording the consent interactor's full outcome: `engine_name`/
  `engine_version`, `banner_present`, `detected_cmps`, `matched_popup_cmp`,
  `action_requested`, `decision_status` (`not_attempted` for
  `no_interaction`; `no_banner`, `applied`, `failed`, or `timeout` for
  `reject_all`/`accept_all`; `instrument_error` for any of the three when
  the call itself never produced a real engine result), the raw engine
  messages, `clicks`, and `duration_s`. A failed or timed-out decision is
  never reported as `applied`, and a visit with no banner is
  distinguishable from one where a decision silently did nothing.
  `instrument_error` is kept apart from `failed`: the latter means the
  engine ran and reported its own decision unsuccessful, the former means
  the harness never got a real result to read.
- `block-verdicts.jsonl` and `exclusion-report.json` -- written by the
  blocked-visit detection pass below, not by the crawl itself; absent until
  that pass has run against the cell at least once.

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

## Consent interaction

Detection and, for `reject_all`/`accept_all`, the decision itself run
through `dv_tracking.crawl.consent_interaction.ConsentInteractor` -- an
interface, not a hardcoded engine. `ConsentInteractionCommand` calls
`interactor.run(webdriver, action, timeout_s)` at the same point in the
sequence for every condition; `action="none"` runs the identical
detection path and simply never requests a decision, which is what keeps
the three arms' pre-consent phases comparable. The command is unaware of
which library backs the interactor.

`AutoconsentInteractor` is the current implementation, built on a pinned
release of DuckDuckGo's `autoconsent` (vendored in
`tracker_lists/autoconsent/`; see that directory's `PROVENANCE.md`).
Detection and decision share one bounded call because the engine itself is
built that way: `AutoConsent._start()` always detects first and only acts
when a decision is configured, so there is no separate round-trip that
could see a different DOM than the one the decision was made against.
Two choices follow from the vendored engine's own defaults, not from a
documentation read: `enablePrehide` is forced `false` (upstream defaults
it to `true`, which hides matched banner elements as a side effect of
merely looking for one) and `detectRetries` is capped at `1`, because the
engine's own `findCmp()` waits for a DOM mutation on every retry past the
first, with a 60-second default timeout and no config flag to shorten or
disable it -- confirmed by running the built script against a synthetic
no-banner page, where a `detectRetries` of 5 reported `timeout` well
before the engine's own retry loop gave up. By the time this command
runs, the page has already sat through the arm's pre-dwell (>=10s), which
is where an asynchronously-loaded banner is expected to have already
appeared.

`decision_status` is `not_attempted` (no decision requested),
`no_banner` (nothing to act on), `applied`, `failed`, `timeout`, or
`instrument_error` (a WebDriver/script fault, or an unparseable script
result -- no real engine result was ever obtained) -- never collapsed
into each other, so a silently failed click cannot look like a clean
success or an absent banner, and a harness fault cannot look like a
measured consent outcome. `clicked_at_utc` comes from the
engine's own `state.endTime` on a successful decision, not a timestamp
taken after the fact; `category_targets` is always `None` for this
engine, which exposes an opt-out/opt-in decision per CMP rather than a
per-category toggle. The vendored `rules.json` (1,327 CMP entries) covers
`Termly`, `cookieyes`, `cookieyes-legacy`, and `wix` among this project's
ten pilot sites; it does not cover Elementor's cookie widget or the
Enfold/Avia theme consent -- a measured coverage gap, not something
patched locally.

The vendored Consent-O-Matic assets (`tracker_lists/consentomatic/`)
stay in place as an independent presence detector consumed by
`dv_tracking.classify.banner`, not as the decision engine; two detectors
disagreeing on the same page is calibration evidence, not something this
command reconciles.

## Response-body capture

`--save-content-types` (also readable from `DV_SAVE_CONTENT_TYPES`) sets
`BrowserParams.save_content` to a comma-separated subset of OpenWPM's
resource types, and refuses at argument parsing, before any browser opens,
if a type in that list is not one OpenWPM recognises. The default is
`main_frame,sub_frame,script,xmlhttprequest`: `main_frame` is the page
HTML the CMP-presence probe classifies; `sub_frame` because consent
platforms commonly render their banner in an iframe, and skipping it would
systematically miss the thing the probe needs to see; `script` for the
CMP's own code; `xmlhttprequest` for configuration it fetches after load.
Images, media, fonts and stylesheets are left out to keep the store
tractable, and a calibration pass that needs them can widen the set
without a code change.

A smoke run of 10 visits (5 UK, 5 US, `no_interaction` only) put 1,647
third-party requests through this join, of which 928 (56%) carry a
resource type in the default set. Summing the `Content-Length` header on
those 928 responses gives 14.3 MB, about 1.43 MB per visit; scaled to a
full sweep (500 sites x 3 arms x 1 replicate = 1,500 visits) that is
roughly 2.1 GB. Two things pull against that figure in opposite
directions: about a third of the matched responses carry no
`Content-Length` at all (chunked or otherwise undeclared), so the header
sum is a lower bound on true bytes served; and the store is
content-addressed by `content_hash`; a script served identically across
many sites (a CMP vendor's bundle, a shared CDN library) is written once,
not once per site, so the true stored size will be smaller than linear
scaling implies. Net, a full sweep under the default type set should land
in the low single-digit gigabytes -- not a capacity concern for local
disk, but worth watching as the content store actually fills, since
dedup's effect cannot be sized precisely before that run.

## Blocked-visit detection

A site sometimes serves one exit a page and another a block: a WAF entry
in this corpus returned 205 requests on one residential exit and a 403
page with 22 on another, with nothing in the cookie count hinting at the
difference. `python -m dv_tracking.crawl.detect_blocks <cell_dir>` classifies
every visit in one cell against three routes, tried in that order:

1. The main document's own response status -- any 4xx/5xx blocks, named by
   the actual status (`status_403`, `status_503`, ...).
2. A known WAF/CDN header or request host, read straight out of
   `crawl-data.sqlite` (BitNinja's `admin.bitninja.io`, Cloudflare's
   `cf-mitigated: challenge` header or `challenges.cloudflare.com`, Akamai's
   `akamai-grn`, Imperva/Incapsula's `x-iinfo` or `*.incapsula.com`,
   DataDome's `geo.captcha-delivery.com`).
3. A title/body interstitial signature over the retained main-document HTML
   (`dv_tracking.crawl.interstitial`), read from the content store -- the
   only route that needs it, and the only one `signature_assessable` can be
   false for. A cell crawled with content capture off (or a resource-type
   set that excludes `main_frame`) still gets routes 1 and 2; route 3 reports
   `not_assessable` rather than guessing.

A site is usable only if every arm it attempted was served: one blocked arm
makes the site's contribution to the consent-arm comparison unusable, not
just that arm, so dropping only the blocked visit is wrong. The rule is a
pure function over a set of visit verdicts and does not special-case the
arm count, so it holds unchanged now that all three of `no_interaction`,
`reject_all`, and `accept_all` are active.

This module imports `plyvel` (the content-store reader only) and therefore
runs inside the crawl container, same as `openwpm_run.py`:

```bash
docker run --rm --platform linux/amd64 \
    -v "$(pwd)/src:/opt/dv-tracking/src" -v "$(pwd)/data:/data" \
    -e PYTHONPATH=/opt/OpenWPM:/opt/dv-tracking/src \
    dv-crawl-crawl:latest python -m dv_tracking.crawl.detect_blocks \
    /data/raw/<crawl_date>_<geolocation>/openwpm
```

It writes `block-verdicts.jsonl` (one row per visit, with every signal that
fired and the first-party/total request counts as auxiliary evidence) and
`exclusion-report.json` (per-arm and per-site counts by reason code, plus
the excluded site list) into the cell directory, overwriting both in full
on a re-run. Both are plain JSON; the analysis stage, which runs outside the
container, reads them with no `plyvel` dependency.

First-party request count is carried as evidence, never as a classifier
input: on this corpus's own pilot pair, cookie counts moved only 18% on a
genuine block (17 to 14) while first-party requests moved 99% (159 to 2), so
request volume is diagnostic where cookie count is not -- but a genuinely
light page is a legitimate outcome, not a block, so the count stays
informational rather than decisive. reCAPTCHA presence alone is deliberately
not a signal either: this corpus's sites routinely carry it on contact and
self-referral forms, so the body-signature matcher requires anti-bot title
wording alongside the widget before it counts a reCAPTCHA page as a
challenge.

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
