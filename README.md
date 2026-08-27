# us-uk-dv-tracker-cookies-research

Work in progress.

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

## Sites & Results
Available upon request to verified researchers. Message me or email me with the subject [UK-US DV Data Request]: josid@ridol.fo
