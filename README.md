# us-uk-dv-tracker-cookies-research

Work in progress.

A sector-specific web-measurement study of pre-consent third-party tracking on
United Kingdom and United States domestic violence support websites. The study
visits each site with a fresh browser, records the network requests, cookies,
and storage set before any consent interaction, and compares the two
jurisdictions at an aggregate level.

## The relay and the exit gate

The residential-exit credentials belong to a relay sidecar, not to this
codebase: it terminates an unauthenticated listener and attaches the
upstream username, password, and routing parameters itself, so no credential
crosses into Python. Those credentials -- `IPROYAL_USERNAME`,
`IPROYAL_PASSWORD`, `IPROYAL_COUNTRY`, `IPROYAL_SESSION` -- belong in the
git-excluded `.env` at the repo root that the relay container reads; it
refuses to start without them. The relay sidecar itself is not present in
this tree yet.

The pre-crawl exit gate (`dv-check-exit`) reads `DV_RELAY_HOST` and
`DV_RELAY_PORT` from the same `.env` (or the exported environment, which
wins) to find the relay's listener. Both are required with no default, so an
unset relay address fails the gate closed rather than resolving to some other
listener on the machine.

Neither the relay sidecar nor a crawl entry point lives in this tree yet, so
the raw write path (`RawStore`, a trial's `metadata.json` and its `exit_info`
object) currently has no producer, mid-cutover from the removed Playwright
crawler to an OpenWPM one.

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
