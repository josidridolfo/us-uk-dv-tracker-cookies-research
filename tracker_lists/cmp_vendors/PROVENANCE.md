# CMP vendor host list provenance

Hand-curated for this study. The list maps consent-management-platform (CMP)
vendors to the third-party host patterns their consent assets load from, so the
banner classifier can register a *vendor_observed* signal (a pinned CMP host
loaded, gate not necessarily API-verified this visit).

- List version: `cmp-vendors v1`
- Compiled: 2026-07-25
- File: `cmp_vendors.csv`
- Schema: `vendor,host_pattern,match_mode,precision`
  - `match_mode`: `suffix` matches the host itself or any subdomain of it
    (`host == pattern or host.endswith("." + pattern)`); `substring` matches the
    pattern anywhere in the host. All current rows use `suffix`; `substring` is
    reserved for future host tells that embed the vendor token mid-label.
  - `precision`: `high` for CMP-dedicated hosts; `low` for dual-purpose vendors
    whose host also serves non-consent products (see the false-positive note).

## Sources (union of three)

The list is the union of three sources, cross-checked against each other.

### 1. Wappalyzer "Cookie compliance" fingerprints — host-pattern backbone

The community `webappanalyzer` fork of the Wappalyzer technology database.

- Source: https://github.com/enthec/webappanalyzer (`src/technologies/*.json`)
- Pinned commit: `345fb11624f865649a8ba33ce709065b9a0e575a`
- Commit date: 2026-07-15
- Category: `Cookie compliance` (category id `67`) — 83 technologies.
- Extraction: each technology's `scriptSrc` regexes were reduced to their
  registrable serving host (e.g. OneTrust `cdn.cookielaw.org`, Civic
  `cc.cdn.civiccomputing.com`, Cookie-Script `.cookie-script.com`, Sirdata
  `js.sddan.com` / `*.consentframework.com`). This source is authoritative for
  the non-TCF standalone tools the IAB registry omits.
- License: MIT (Wappalyzer fingerprint data).
- Not host-mappable from this source (self-hosted plugins / bundled scripts with
  no distinct third-party host — detectable only via DOM/heuristic, never the
  vendor_observed tier): Complianz, Moove GDPR, WebToffee GDPR, Cookie Notice
  (WP), CookieBAR, Spatie Laravel Cookie Consent, Yett, Segment Consent Manager
  (bundled), Shopify native consent, AdRoll CMP.

### 2. IAB Europe TCF CMP list — NAME cross-check only (no hosts)

- Source: https://cmplist.consensu.org/v2/cmp-list.json
- Snapshot: 2026-07-23 (`lastUpdated` 2026-07-23T16:00:25Z), 311 registered CMPs.
- Use: this list carries only `id` + `name` + `isCommercial` + `environments`,
  **no hostnames**, and it omits non-TCF standalone tools (it lists CookieYes and
  CookieHub but NOT Cookie-Script or Civic Cookie Control). It is used solely to
  confirm no major *registered* CMP is missing from the list; any missing name is
  then mapped to its serving hosts from source 1 or vendor docs. It is **not**
  treated as a host source.

### 3. Empirically-observed hosts from this study's crawl — ground truth

- Source: `data/processed/per_request_findings_2026-07-25_{uk,us}.parquet`.
- Every CMP-vendor host actually loaded on the corpus is covered with its exact
  observed registrable domain: `cdn-cookieyes.com`, `civiccomputing.com`,
  `clym.io`, `clym-sdk.net`, `consentmanager.net`, `cookie-script.com`,
  `cookiebot.com`, `cookiehub.net`, `cookieyes.com`, `gettermscmp.com` (Termly),
  `iubenda.com`, `privacymanager.io` (LiveRamp PCM), `termly.io`,
  `usercentrics.eu`, `userconsent.org`.
- Excluded after inspection as non-CMP: `trustedsite.com` (McAfee SECURE / trust
  badge), `youtube-nocookie.com` (Google privacy-enhanced embed).

## False-positive discipline

A larger list widens the false-positive surface, so dual-purpose vendors match
their CMP-specific host ONLY:

- **Quantcast**: `cmp.quantcast.com` and `quantcast.mgr.consensu.org` only —
  never bare `quantcast.com` / `quantserve.com` (Quantcast Measure analytics).
- **ShareThis**: `cmp.sharethis.com` only. In this study's crawl `sharethis.com`
  appeared solely as the social-sharing widget (`buttons-config.sharethis.com`,
  `platform-api.sharethis.com`), which is NOT the CMP; bare `sharethis.com` is
  therefore excluded.
- **Sirdata**: the CMP hosts `sddan.com` / `consentframework.com`, not the
  `sirdata.com` data-broker host.
- **Funding Choices**: the full host `fundingchoicesmessages.google.com`, never
  bare `google.com`.
- **OneTrust Optanon blob**: the exact host `optanon.blob.core.windows.net`,
  never bare `blob.core.windows.net`.
- Flagged `precision: low` (dual-purpose or shared corporate domain): Iubenda,
  ShareThis, Tealium (tag manager), Klaro (`kiprotect.com`), Pandectes, Metomic,
  Q4 Cookie Monster.

## Ordering requirement

`hostname_signal` returns the first matching row, and the specific
`*.mgr.consensu.org` subdomains also end with the generic `consensu.org`. Keep
the specific consensu.org subdomains (Quantcast, Conversant, UniConsent, Clickio)
above the generic `IAB TCF runtime,consensu.org` catch-all row, which stays last,
so a registered vendor wins over the generic TCF-runtime attribution.
