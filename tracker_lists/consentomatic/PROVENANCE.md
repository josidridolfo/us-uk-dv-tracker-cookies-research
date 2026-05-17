# Consent-O-Matic detection assets - vendoring

The cookie-banner detector uses upstream Consent-O-Matic to recognise a
Consent Management Platform from the DOM during the pre-consent window
(see `src/dv_tracking/classify/banner.py`). Two upstream files are
vendored byte-for-byte; the third is a small wrapper
that exposes a presence-only entry point.

## Source

- Repo: https://github.com/cavi-au/Consent-O-Matic
- Pinned commit: `8ca8500d26434c586039e126ad091e1bfccd205d`
- Commit date: 2025-11-07T14:52:39Z
- License: MIT (`LICENSE` matches upstream)

## Vendored files

Vendoring date: 2026-05-16.

| File | Bytes | Description |
|---|---:|---|
| `Rules.json` | 377,252 | Consolidated upstream rule set, 44 CMPs. All `presentMatcher` filters preserved (`textFilter`, `displayFilter`, `iframeFilter`, `styleFilters`, `childFilter`, `parent`). |
| `Tools.js` | 8,421 | Upstream `Tools` class with `find()`. ES-module `import` and `export` are stripped at JS-build time by the crawler; the on-disk file is byte-exact. |
| `rules.schema.json` | 14,206 | Upstream JSON schema. Reference-quality only; not consulted at runtime. |
| `LICENSE` | 1,186 | MIT text. |

## Wrapper

`cmp_present_probe.js` is a presence-only wrapper that loops over
rule `detectors[]`, calls upstream `Tools.find()` on each `presentMatcher`,
and returns the matched rule keys. Pre-consent DOM interaction is out of
scope, so upstream action / consent-method machinery isn't invoked.
```
