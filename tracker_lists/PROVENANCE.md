# Tracker list provenance

Third-party lists change all the time. To keep the study reproducible,
snapshots are vendored into this repo with the version + date recorded.

## Open Cookie Database

- Snapshot date: 2026-06-13
- Source: https://github.com/jkwakman/Open-Cookie-Database
- Vendored: `tracker_lists/open_cookie_database/open-cookie-database.csv`
  (~570 KB; 2,264 catalogued cookies, one row per Platform x Cookie).
- Used as the cookie-classification source of truth for the
  ePrivacy-relevant taxonomy (`strictly_necessary`, `functional`,
  `analytics`, `marketing`, `preferences`).
- License: CC BY 4.0

## Consent-O-Matic CMP detector rules

- Snapshot date: 2026-05-16
- Source: https://github.com/cavi-au/Consent-O-Matic
- Pinned commit: `8ca8500d26434c586039e126ad091e1bfccd205d`
- Rules: 44 CMP entries; every `presentMatcher` field preserved verbatim.
- Full detail: `tracker_lists/consentomatic/PROVENANCE.md`.
- License: MIT
