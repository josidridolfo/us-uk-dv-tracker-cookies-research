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

## DuckDuckGo autoconsent

- Snapshot date: 2026-10-30 (pinned npm version `v17.1.0`)
- Source: https://github.com/duckduckgo/autoconsent
- Rules: 1,327 CMP entries from the packaged `rules/rules.json`.
- Drives the live consent-interaction decision (detect + opt-out/opt-in);
  `tracker_lists/consentomatic/` stays as an independent presence detector
  only, not the decision engine.
- Full detail: `tracker_lists/autoconsent/PROVENANCE.md`.
- License: MPL-2.0

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

## Mozilla Public Suffix List

- Snapshot date: 2026-10-28 (the list's own `// VERSION: 2026-10-28_13-15-55_UTC`)
- Source: https://publicsuffix.org/list/public_suffix_list.dat
- Pinned commit: `9dfbe81931367b3f0af21035c36ae7eeb830dd88`
- Vendored file: `tracker_lists/public_suffix_list/public_suffix_list.dat`
  (~330 KB; 16,563 lines across the ICANN and private sections).
- SHA-256: `1f96bae40adfc22ca2474d7c0099bbcfcb25aefd3e2547b5c2147c0dd2e3ebd9`
- Resolves every registered domain in the analysis stage, which is what
  first-party and third-party mean in this study. `tldextract` fetches a live
  list by default and caches it, so results would otherwise depend on when the
  analysis ran; `dv_tracking.analysis.party` reads this file and nothing else,
  with `fallback_to_snapshot=False` so an absent file fails instead of silently
  reverting to the copy bundled in the installed `tldextract`.
- Only the ICANN section is treated as registrable
  (`include_psl_private_domains=False`), so `someone.blogspot.com` resolves to
  `blogspot.com` and a tracker reached at `bucket.s3.amazonaws.com` counts
  against `amazonaws.com`. The reason is party counting: one vendor spread over
  many buckets or subdomains of a hosting suffix is one party, not many. The
  opposite choice is defensible for attributing a *site* to its operator, which
  is not what this list is used for here.
- The pin is checked, not assumed. The file's `// VERSION:` and `// COMMIT:`
  header must equal `constants.PUBLIC_SUFFIX_SNAPSHOT`, and its SHA-256 must
  match the digest above, which is what catches an edited or truncated file
  whose header still looks right. Party resolution raises
  `PublicSuffixPinError` on either mismatch. Refreshing the snapshot means
  updating all three fields and re-running the analysis.
- The Playwright-era `dv_tracking.classify` path does **not** use this list. It
  resolves through `dv_tracking.utils`, against whichever snapshot the installed
  `tldextract` bundles. Two party rules therefore coexist until that path is
  retired, and only this one is pinned.
- Not copied into the crawl image: the crawl container has no `tldextract` and
  never resolves a registered domain.
- License: MPL-2.0
- Vendored verbatim, including the private section this project does not use.
  Editing the file would break its correspondence with the published snapshot
  and with the digest above, so the two pre-push greps need documented
  exceptions for it:
  - Lines 1-6 are Mozilla's MPL-2.0 header, which contains "You can obtain one
    at". The only second-person match in the vendored list data.
  - Lines 12389-12393 are a private-section registry entry whose registrant
    happens to be an AI vendor, so the file matches a grep for that vendor's
    name. These are Mozilla's records of a third party's domains, carrying no
    information about how this repository was produced. The same grep already
    matches the Tracker Radar index, the EasyPrivacy rules and the autoconsent
    rules, for the same reason: a domain list names whoever registered a domain.
  - The private section is parsed and discarded
    (`include_psl_private_domains=False`). Vendoring the ICANN section alone
    would remove both matches, at the cost of a file that can no longer be
    checked against the published list.
