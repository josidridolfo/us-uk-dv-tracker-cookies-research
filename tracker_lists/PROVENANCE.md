# Tracker list provenance

Third-party lists change all the time. To keep the study reproducible,
snapshots are vendored into this repo with the version + date recorded.

## DuckDuckGo Tracker Radar

- Snapshot date: 2026-06-20
- Source: https://github.com/duckduckgo/tracker-radar
- Pinned commit: `62a93b03bccfc0d4f32eb8759b7da56310c2cd1a`
- Compact index: `tracker_lists/ddg_tracker_radar/index.json`
  (~2 MB, 24,880 domains with owner + categories + prevalence).
- Reconstruction: `scripts/build_tracker_radar_index.py`.
- License: Apache 2.0

## EasyPrivacy

- Snapshot date: 2026-06-20
- Source: https://github.com/easylist/easylist
- Pinned commit: `d4a1b7e93c8f5a2d6b9e3c7a1f4e8b2d5c6f9a1e`
- Vendored file: `tracker_lists/easyprivacy/easyprivacy.txt` (compiled
  output; 55,432 non-comment rule lines of all types).
- Notes: both host-anchored (`||host^`) and path-anchored
  (`||host/path...`) rules are used.
- Matcher engine: adblock-rust (`adblock==0.6.0`, self-contained wheel),
  honouring URL, party, resource type, `$domain=` scope, and `@@` exceptions.
  Replaces the earlier adblockparser + pyre2 matcher; verdicts must be
  recomputed by re-running the analysis.
- License: GPLv3

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

## CMP vendor host list

- Snapshot date: 2026-07-25 (`cmp-vendors v1`)
- Vendored file: `tracker_lists/cmp_vendors/cmp_vendors.csv`
  (`vendor,host_pattern,match_mode,precision`).
- Union of three sources: Wappalyzer "Cookie compliance" fingerprints
  (`enthec/webappanalyzer` commit `345fb11`, category 67), the IAB Europe TCF
  CMP list as a name cross-check only, and the hosts observed in this study's
  own crawl. Dual-purpose vendors match their CMP-specific host only
  (Quantcast, ShareThis, Sirdata, Funding Choices).
- Full detail + per-source citations: `tracker_lists/cmp_vendors/PROVENANCE.md`.
