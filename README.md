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

That margin is a function of the recheck cadence and the session
lifetime alone, not of how long the cell runs or how many visits it
holds, so tripling the visit count under `REPLICATE_COUNT=3` changes
neither the margin nor what one exit check means for a visit. How many
visits sit behind a given check is `exit_recheck_seconds / visit_duration`
(roughly 3-5 at the 60-90s/visit measured figure and the 300s default), a
ratio that stays fixed as the cell grows; a longer cell just runs more
checks over more wall-clock time, each still governing the same small,
constant-sized run of visits that follow it until the next one.

One cell is `data/raw/<crawl_date>_<geolocation>/openwpm/`, holding:

- `crawl-data.sqlite` -- OpenWPM's own primary artifact and the resume ledger.
- `content-store.leveldb/` -- OpenWPM's own unstructured content store, a
  LevelDB database (LevelDB's own format is a directory, not a single
  file, hence the directory living beside `crawl-data.sqlite` rather than
  as a sibling file) that this project never opens for writing. Keyed by
  `content_hash`, the same value recorded on the `http_responses` row --
  but that key is *not* the sha256 of the stored bytes. It is the sha256
  of only the first chunk the browser extension streamed for that
  response (`Extension/src/lib/response-body-listener.ts`'s
  `resolveContentHash`, a promise resolver that only the first `ondata`
  call ever settles). Chunk boundaries are a function of network timing,
  not of the body, so the exact same bytes fetched twice can register
  under two different keys and get written to this store twice. On the
  one archived cell measured so far, 583 keys held only 424 distinct
  bodies -- 159 duplicates, 42% of the store's 60.2 MB.
- `content-store-dedup.leveldb/` -- written by
  `dv_tracking.crawl.compact_content_store`, a separate post-crawl pass,
  not by the crawl itself; absent until that pass has run against the
  cell. A body lives once, under its own real sha256 (`body:<digest>`),
  and `hash:<content_hash>` maps to the digest that resolves it. See
  Content-store compaction below.
- `content-store-compaction.json` -- written alongside it: the blob and
  byte counts before and after, duplicates removed, and the result of the
  pass's own losslessness check, so a compacted cell carries the proof
  rather than asking a reader to trust it. Absent until compaction has run.
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
  conditions and their dwells, `replicate_count`, the `load_anchor` method
  and its timeout, the finalize drain tail, the sites file and its hash,
  and other facts that can legitimately differ between a cell's
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
- `loads.jsonl` -- one line per visit from `LoadAnchorCommand`, the
  command that runs right after the page's `get()` and right before the
  fixed pre-consent dwell. Records `load_complete_utc` (the instant an
  explicit poll of `document.readyState` first observed `"complete"`, or
  the instant its own bound expired, whichever came first),
  `load_anchor` (`"document_ready"`, the one anchor implemented today),
  `load_anchor_timed_out` (whether that bound was hit before the page
  reported complete), `load_wait_s` (that instant minus the preceding
  `StampVisitCommand` timestamp -- the page-load-time component this
  design pulls out of the dwell rather than folding into it), and
  `last_error` (the most recent poll's own exception, if any, so a timed-
  out row is distinguishable from a page that was simply still loading).
- `boundaries.jsonl` -- one line per visit recording `boundary_utc`: the
  consent interactor's own reported click instant for a successful
  `reject_all`/`accept_all` decision (`boundary_source: "engine_reported"`),
  or this command's own timestamp otherwise -- `no_interaction`, no banner
  detected, or a failed/timed-out attempt (`boundary_source: "synthetic"`).
  `offset_from_get_start_s` is `boundary_utc` minus the preceding
  `StampVisitCommand`'s timestamp, and still carries load-time and
  consent-detection-time variance by construction -- it is kept because it
  is the quantity that first showed the pre-consent window was a different
  length at every site. `dwell_window_s` is `boundary_utc` minus
  `loads.jsonl`'s own `load_complete_utc` for the same visit: the quantity
  the load-completion anchor exists to hold close to `pre_dwell_s` plus a
  small, site-independent consent-detection remainder, across every site.
  `load_complete_utc` and `load_anchor_timed_out` are carried through from
  `loads.jsonl` alongside it, and all three of the new fields are `None`
  when no matching `loads.jsonl` row exists -- a defensive case kept for a
  future reordering of the sequence, since today OpenWPM aborts a
  sequence on any non-`"ok"` command, so `ConsentInteractionCommand` only
  ever runs once `LoadAnchorCommand` has already run to completion and
  written its row. An unanchored window stays `None`, not a silently
  assumed normal one, if that ever stops holding.
- `interactions.jsonl` -- one line per visit from the same command
  (`"record_type": "decision"`), recording the consent interactor's full
  outcome: `engine_name`/
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
  the harness never got a real result to read. The same file also carries
  one `"record_type": "screenshot"` row per capture, from
  `ConsentScreenshotCommand` -- see Consent screenshots below.
- `screenshots/` -- OpenWPM's own directory (`TaskManager` always points
  `ManagerParams.screenshot_path` at `<data_directory>/screenshots` and
  creates it, whether or not anything writes there); see Consent
  screenshots below for what it holds and how its files are named.
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
The drain tail is unaffected by anything below: it still starts after
`post_dwell_s`, measured from the consent boundary exactly as before.

## Pre-consent dwell

A visit's sequence is: get the page, confirm and stamp load completion,
sleep `pre_dwell_s`, run the consent command, sleep `post_dwell_s`. The
middle two steps used to be one: `GetCommand`'s own internal sleep ran
immediately after `webdriver.get()` returned, so `pre_dwell_s` meant "load
time plus the configured number of seconds," and load time measured on a
ten-site sample varied by a factor of three -- the pre-consent observation
window was a different length at every site, and on one visit three of a
site's ten third-party domains (one of them a geolocation lookup, exactly
the kind of request that fires once a page has settled) were missing
entirely, present on every other visit of the same site.

`LoadAnchorCommand` now sits between the get and the dwell. It polls
`document.readyState` explicitly, under its own bound
(`LOAD_ANCHOR_TIMEOUT_S`, 15 seconds plus a 5-second margin against the
sequence-level watchdog), and stamps `load_complete_utc` the instant it
first sees `"complete"` -- or, failing that, the instant its own bound
expires, with `load_anchor_timed_out: true` so that visit's window reads
as unanchored rather than as a normal one. The dwell then sleeps
`pre_dwell_s` from that stamped instant rather than from whenever `get()`
happened to return. In the ordinary case `get()` has already blocked on
the same `"complete"` condition (Selenium's default "normal" page-load
strategy), so the poll resolves on its first check and costs nothing; it
only matters on the edge case the fix targets, a `TimeoutException`
`GetCommand` swallows internally that hands control back before the page
is actually done.

`pre_dwell_s` stays at 10.0 under this change. The number was never the
problem -- where its clock started was -- so changing it alongside the
anchor would conflate two different corrections and make neither
auditable. `boundaries.jsonl`'s `dwell_window_s` (boundary minus
`load_complete_utc`) is the quantity to check that the fix worked: it
should sit close to `pre_dwell_s` plus a small, site-independent
consent-detection remainder, at every site, where `offset_from_get_start_s`
(kept, unchanged) still would not.

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

## Consent screenshots

`interactions.jsonl`'s `decision_status` is the engine's own report of
what it did; on a handful of pilot sites a site's own consent cookie
corroborates it independently -- Wix's `consent-policy` cookie reads
`func/anl/adv` at 0 under `reject_all` and 1 under `accept_all`, and a
WordPress consent plugin's five `wp_consent_*` cookies read `deny` then
`allow` -- but on others, Termly and HubSpot among them, no comparably
readable record exists and `decision_status` is the only evidence there
is. `ConsentScreenshotCommand` gives a human a way to check that evidence
against what was actually on screen.

Two viewport screenshots bracket `ConsentInteractionCommand`, for every
condition including `no_interaction`: one with `stage="pre"`, run after
the fixed pre-consent dwell and before the decision call, showing the
banner as the engine saw it; one with `stage="post"`, run right after the
decision, showing whether it actually cleared. Running both in every arm,
not only `reject_all`/`accept_all`, keeps the arms comparable and gives a
`no_interaction` baseline image of every banner nobody clicked -- what a
human validates the engine's own detection against.

Viewport, not full page: OpenWPM's `ScreenshotFullPageCommand` scrolls to
the bottom of the page, in increments of `window.innerHeight`, to stitch a
full-page image. Scrolling triggers viewport-based lazy loading, which
would inflate the pre-consent request count the study measures, and in
the `no_interaction` arm a scroll can itself read as implied consent to a
page listening for one. A banner is a viewport-fixed overlay in any case,
so a viewport capture is where it is, and a full-page capture would show
page content instead.

`ConsentScreenshotCommand` calls `webdriver.save_screenshot` directly
rather than going through OpenWPM's own `SaveScreenshotCommand`: that
command lets a WebDriver fault raise, and BrowserManager breaks out of an
entire command sequence on any non-"ok" command, so a capture fault there
would cost the whole visit over a missing image. Every exception here is
caught and written to `interactions.jsonl` as a `"record_type":
"screenshot"` row with `ok=false` and the error text, `path=null`, instead
-- a visit whose screenshot failed is distinguishable from one a
pre-screenshot cell never attempted it on, and from one that succeeded.

Filenames follow OpenWPM's own `SaveScreenshotCommand` naming convention
(`<visit_id>-<md5 of current_url><suffix>.png`), with `suffix` set to
`<condition>-r<replicate>-<stage>` (e.g.
`142-3a7f2b8e1c9d4f5a6b7c8d9e0f1a2b3c-reject_all-r2-pre.png`) so arm,
replicate, and stage are readable from the filename alone, without a
`visits.jsonl` join, the way a human scanning nine images per site per
capture point (`REPLICATE_COUNT=3` x 3 arms) needs. They land in
`screenshots/`, the directory `TaskManager` already points
`ManagerParams.screenshot_path` at and creates for every cell, whether or
not anything used to write there.

The `stage="pre"` capture's own latency runs between the fixed
pre-consent dwell and the call that stamps `boundary_utc`, so it adds
directly to `boundaries.jsonl`'s `dwell_window_s` for that visit -- the
same quantity the load-completion anchor (see Pre-consent dwell above)
exists to hold equal across sites. No placement between the load anchor
and the decision avoids this without moving the dwell anchor itself,
which is out of scope here, and a placement outside that interval would
either capture a page not yet settled or miss the banner the capture
exists to show. Measured against a Chromium/Playwright page (not the
crawl's own Firefox/geckodriver stack -- geckodriver was not available to
measure locally, so this is an order-of-magnitude proxy, not the real
number) at the crawl's own 1366x768 window size, five real, non-corpus
pages gave a single viewport capture in 26-184ms, against a 10-second
`pre_dwell_s`: the same order of magnitude as the "small,
site-independent consent-detection remainder" `dwell_window_s` already
tolerates by design. The addition is recorded, not estimated:
`ConsentScreenshotCommand` writes its own `duration_s` per capture, so it
can be read back and, if it ever matters, subtracted out rather than
staying an invisible constant.

Per-image size is likewise measured against that same proxy, not
estimated: the five pages above produced PNGs of 74-191 KiB, mean about
101 KiB. At `REPLICATE_COUNT=3` and the full corpus (500 sites x 3 arms x
3 replicates = 4,500 visits, two captures each), 9,000 images at that
mean scale to roughly 0.9 GB -- additive to, not replacing, the
content-store figures in Replicates below. A real Firefox/geckodriver
capture may differ in size or cost from this proxy; the figures above are
what was actually measured, not a substitute for measuring the real
stack once a crawl runs.

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
those 928 responses gives 14.3 MB, about 1.43 MB per visit; at
`REPLICATE_COUNT=1` that scaled to a full sweep (500 sites x 3 arms x 1
replicate = 1,500 visits) of roughly 2.1 GB by this header-sum method; see
the Replicates section below for the current, tripled total under a
separate, independently-measured per-visit figure (content store, not
header sum). The header-sum figure is a lower bound on true bytes served
on its own terms -- about a third of the matched responses carry no
`Content-Length` at all (chunked or otherwise undeclared) -- but it is
not a lower bound on what `content-store.leveldb` holds on disk: that
store is keyed by `content_hash`, which identifies a response, not a
body (see Content-store compaction below), so a script served
identically across many sites is written once per `content_hash` it
happened to land under, not once overall. The one archived cell measured
so far held 42% more bytes than its distinct bodies required before
compaction.

## Content-store compaction

`http_responses.content_hash` is OpenWPM's own identifier for a response,
not a hash of its body. `Extension/src/lib/response-body-listener.ts`
hashes only the first chunk `filterResponseData` hands its `ondata`
listener (`resolveContentHash` is a promise resolver; later chunks never
reach it), while the full body keeps accumulating underneath,
unaffected, into what actually gets sent and stored. Chunk boundaries are
a function of network timing, not of the body, so the same bytes fetched
twice can land under two different keys, and
`LevelDbProvider.store_blob` -- keyed on that value, not on the bytes it
is given -- writes the same body twice.

`dv_tracking.crawl.compact_content_store` is a post-crawl pass that
removes that duplication without touching the crawl's own output. It
never opens `content-store.leveldb` for writing; it reads every key in
it and writes a sibling, `content-store-dedup.leveldb`, where a body
lives once under `body:<its own real sha256>` and `hash:<content_hash>`
maps to the digest that resolves it. The sibling is what a disk-saving
pass is allowed to risk -- the original is the only real capture this
project holds, and is never replaced, deleted, or deduplicated in place.

Before it reports success, the pass checks its own result rather than
assuming it: that the set of distinct body digests after compaction is
exactly the set the original bytes hash to, and that every
`content_hash` in `http_responses` resolves, through the sibling store,
to the exact bytes it resolved to before. Either check failing raises
and no `content-store-compaction.json` is written, so a compacted cell
without that sidecar is one the pass did not vouch for, not one that
silently has the wrong bytes behind some key. Running the pass again is
safe and reports `nothing_to_do: true`: a key already present with the
right bytes is never rewritten.

```bash
docker run --rm --platform linux/amd64 \
    -v "$(pwd)/src:/opt/dv-tracking/src" -v "$(pwd)/data:/data" \
    -e PYTHONPATH=/opt/OpenWPM:/opt/dv-tracking/src \
    dv-crawl-crawl:latest python -m dv_tracking.crawl.compact_content_store \
    /data/raw/<crawl_date>_<geolocation>/openwpm
```

Measured on `data/raw/_archive/2026-10-30_uk_3arm_settle-timeout/openwpm`
(copied to a scratch directory for the run; the archived cell itself was
never opened for writing): 583 keys held 424 distinct bodies -- 60.22 MB
collapsing to 34.84 MB, 159 duplicates removed, 25.38 MB (42%) saved --
and all 1,327 `http_responses` rows carrying a `content_hash` resolved,
through the sibling store, to the exact bytes they resolved to before.
Running the pass a second time against the same cell wrote zero new keys
and reported `nothing_to_do: true`.

The three cells already on disk (`data/raw/2026-10-30_uk`,
`data/raw/2026-10-30_us`, `data/raw/2026-10-30-geoprobe_uk`) hold this
project's only real crawl data and have not had this pass run against
them; `read_main_document_html` reads their flat layout directly; one was
spot-checked read-only against a scratch copy (603 distinct
`content_hash` values, all present as flat keys, all resolved) to
confirm the fallback path holds on real, not just archived, data.
Compaction is additive and does not touch the original, but running it
against this project's only real corpus is a decision left to whoever
next needs the disk space, not one this pass makes for them.

## Replicates

`REPLICATE_COUNT` is 3: every `(site, condition)` pair is visited three
times, everywhere, not just where a prior run looked unstable. Three gives
a median rather than a mean of two, so one blocked or anomalous visit does
not by itself swing a cell's point estimate, and the per-reason-code counts
in `exclusion-report.json` can distinguish a persistently blocked arm from
a transiently blocked one across three independent attempts instead of
one. Two replicates is the fallback if wall-clock or relay budget forces a
cheaper sweep; one replicate per cell is no longer the design.

At 60-90 seconds per visit (including the browser relaunch between
sequences) and the full corpus (500 sites, 3 arms), replicate count scales
total visits and wall-clock linearly:

| replicate_count | visits | wall-clock             | content store (~5.3MB/visit) |
|-----------------:|-------:|------------------------|-------------------------------|
| 1 (previous)     | 1,500  | ~25.0-37.5 hours       | ~8.0 GB                       |
| 2                | 3,000  | ~50.0-75.0 hours       | ~15.9 GB                      |
| 3 (current)      | 4,500  | ~75.0-112.5 hours      | ~23.9 GB                      |

The per-visit content-store figure (5.3 MB, ~80% JavaScript bodies) is
measured over 15 visits and is a different method from the header-sum
estimate in Response-body capture above; the two should not be added
together.

`build_work_list` places replicate as the outer loop: the work list is
`replicate_count` full passes over every `(site, condition)` pair, each
pass in the same shuffled-site, permuted-block order, rather than a site's
replicates running back to back inside one pass. Back to back would put
them a few minutes apart -- inside one relay session's lifetime and before
any transient server-side state from the first visit has necessarily
cleared -- which defeats the point of repeating the trial; one full pass
apart (`len(sites) * len(conditions)` positions, hours apart in practice)
puts each replicate on, in effect, a different relay session and a cold
revisit of the site.

The resume predicate needs no change for replicates above 1: identity is
already `(site_id, condition, replicate)`, so a cell with 2 of 3
replicates of an arm terminal and one not simply resumes the missing one,
the same as it already did for arms. `retry_number` and `attempt_counts`
are keyed the same way and generalise the same way.

The exclusion rule in Blocked-visit detection below needs no change
either, and that is a deliberate reading, not an oversight: it already
groups by `site_id` alone, so one blocked replicate out of nine now
excludes a site exactly as one blocked arm out of three used to. A site
usable on 8 of 9 attempted visits is still not a site every arm can be
compared across on equal footing with its neighbours, so the stricter
whole-site exclusion is the correct extension of the existing rule, not a
new one.

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

A site is usable only if every visit it attempted was served: one blocked
visit makes the site's contribution to the consent-arm comparison
unusable, not just that visit, so dropping only the blocked one is wrong.
The rule is a pure function over a set of visit verdicts and does not
special-case the arm count or the replicate count, so it holds unchanged
now that all three of `no_interaction`, `reject_all`, and `accept_all` are
active, and unchanged again at `REPLICATE_COUNT=3` (see Replicates above).

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

## Third-party determination

Every exposure figure in this study rests on one decision: whether a request
went to a third party. `dv_tracking.analysis.party` is where the OpenWPM
analysis stage makes it. The Playwright-era `dv_tracking.classify` pipeline,
which is what `dv-analyze` still runs, resolves domains through
`dv_tracking.utils` against whichever public suffix list the installed
`tldextract` bundles; migrating it onto the pinned list is the next step, and
until then the tree carries two party rules and only one of them is pinned.

OpenWPM no longer makes the decision at all.
`http_requests.is_third_party_channel` and `is_third_party_to_top_window` are
NULL on every captured row, because the upstream computation is commented out in
the extension's `http-instrument.ts`: it used the XPCOM `mozIThirdPartyUtil`
service a WebExtension cannot reach. `req_call_stack` is empty for the same era
of reasons. What is populated on 100% of rows is `url`, `top_level_url`,
`loading_origin`, `loading_href`, `triggering_origin`, `frame_id`,
`parent_frame_id`, `frame_ancestors` and `resource_type`, which is enough to
reconstruct both notions upstream once had:

- **`party_to_top_level_site`**: the request's registered domain differs from
  `top_level_url`'s. This is the notion a headline claim about visitor exposure
  uses, being how many distinct companies a visitor to this site meets.
- **`party_to_loading_document`**: the request's registered domain differs from
  `loading_origin`'s. This diverges from the first inside a nested frame, and
  13.7% of requests in the first real cell load inside one, so it is not an edge
  case for a study of consent platforms that render in iframes. It can also
  include the site's own domain, where the site's own endpoint is called from
  inside a third party's frame, which is why it is the wrong notion for a
  visitor-exposure claim.

Registered domains come from the vendored public-suffix snapshot pinned in
`constants.PUBLIC_SUFFIX_SNAPSHOT`, whose digest and self-declared version and
commit header are both verified before the first resolution. `tldextract`
otherwise fetches a live list and caches it, which would make a result depend on
the day the analysis ran. Only the list's ICANN section counts as registrable,
so a tracker reached at `bucket.s3.amazonaws.com` counts against
`amazonaws.com`: one vendor spread across many subdomains of a hosting suffix is
one party, not many. The earlier ad-hoc rule took the last two labels of the
hostname, which reads `womensaid.org.uk` as `org.uk` and so called every
`.org.uk` third party on a `.org.uk` site first-party; on a 250-site UK corpus
that is not a hypothetical.

A host resolves to one of five kinds. A registered domain, an IP literal (its
canonical compressed form, counted as a party in its own right), or a
suffix-less host such as `localhost` (its whole hostname) are all comparable. A
non-network URL (`data:`, `blob:`, `about:`, `moz-extension:`, `javascript:`) is
not treated as a party, and an absent value, a missing netloc or OpenWPM's
`"undefined"` and `"null"` strings are unresolvable. Neither kind takes part in
a comparison, so a request whose reference document cannot be resolved reads
`undetermined` rather than defaulting to third-party, and the per-site count of
such requests is reported alongside the counts as their data-quality
denominator. `blob:` and `filesystem:` do inherit their creator's origin, so
excluding them is the conservative reading rather than a claim about the
schemes.

That exclusion is why a top-level navigation, whose `loading_origin` OpenWPM
writes as `"undefined"`, is first-party to the site and undetermined to its
loading document, instead of contributing the site's own domain to the
third-party count. Case, ports, userinfo, a trailing root dot and the leading
dot of a domain cookie are normalised away, and internationalised hosts resolve
to punycode, so `javascript_cookies.host` values resolve through the same rule
as request URLs and cookie party-ness agrees with request party-ness by
construction. The punycode step runs through the stdlib IDNA2003 codec, which
disagrees with the browser's UTS#46 mapping on a narrow class of hosts (sharp s,
final sigma); Firefox serialises hosts to A-labels before the extension observes
them, so a captured host arrives already normalised and the divergence is not
reachable from crawl data.

Aggregation is two separately named operations returning two different types,
because conflating them has already produced two headline figures for the same
data. Each carries the notion it was built under, so a number cannot be detached
from its definition:

- `per_site_distinct_counts` counts each site's distinct third parties and
  reports the sum, mean and median of those counts, answering how many third
  parties a site in this corpus carries. A domain on five sites counts five
  times.
- `corpus_domain_union` unions those sets, answering how many distinct companies
  appear anywhere in the corpus. Always at most the per-site sum.

On the first real UK cell (four sites, `uk-an` excluded for a WAF-blocked reject
arm), `no_interaction` and `reject_all` each give a union of 21 third-party
domains against a per-site sum of 26, and `accept_all` gives 32 against 50. The
two notions give the same union on that cell. Two domains (`parastorage.com`,
`wixapps.net`) are third-party to the site and first-party within their own
frame, but both still reach the loading-document set through the frame documents
the site's own page loaded, and no site called its own domain from inside a
sub-frame, which is the only thing that would have made the loading-document
union larger.

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
