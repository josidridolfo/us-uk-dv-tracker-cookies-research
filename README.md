# us-uk-dv-tracker-cookies-research

A sector-specific web-measurement study of pre-consent third-party tracking on
United Kingdom and United States domestic violence support websites. The study
visits each site with a fresh browser, records the network requests, cookies,
and storage set before any consent interaction, and compares the two
jurisdictions at an aggregate level.

Each raw trial's `metadata.json` carries an `exit_info` object recording the
pre-crawl gate's observed exit (`ip`, `country`, `org`, `residential`,
`via_proxy`, `checked_at_utc`), constant across every trial in a run, with an
optional supplementary `provider` label.

## Credentials

The IPRoyal residential-exit credentials live in a git-excluded `.env` at the
repo root, holding the keys `IPROYAL_HOST`, `IPROYAL_PORT`, `IPROYAL_USERNAME`,
and `IPROYAL_PASSWORD`. The entry points (`dv-crawl`, `dv-validate-vpn`) load
this file automatically, so no manual `source` step is needed. An already
exported environment variable overrides the file value.

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

Work in progress.
