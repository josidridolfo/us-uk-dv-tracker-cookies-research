# autoconsent - vendoring

The consent-interaction command (`dv_tracking.crawl.consent_interaction`)
detects a consent banner and, for an active consent arm, applies that
arm's decision through DuckDuckGo's `autoconsent` library. Three upstream
files are vendored byte-for-byte; a fourth is reference-quality only.

## Source

- Package: `@duckduckgo/autoconsent` on npm
- Repo: https://github.com/duckduckgo/autoconsent
- Pinned version: `17.1.0` (published 2026-10-30, `latest` at pin time)
- Tarball: `https://registry.npmjs.org/@duckduckgo/autoconsent/-/autoconsent-17.1.0.tgz`
- Integrity: `sha512-qaXi19l4+cDImoXBuWgQiiQmQ00lxGRxhBQOEWnx9DDGcPO6A4/VK1mkAUrDkXCq4IIil/qFErssqvSxBpXVYQ==`
- shasum (sha1): `943ea2432bc9be8e493178376ad79edb2e007b7a`
- License: MPL-2.0 (`LICENSE` matches the tarball's)

## Vendored files

Vendoring date: 2026-10-30.

| File | Bytes | Source path in tarball | Description |
|---|---:|---|---|
| `autoconsent.esm.js` | 157,664 | `dist/autoconsent.esm.js` | The library build: the `AutoConsent` engine class and every CMP-rule interpreter (`AutoConsentCMP`, `AutoConsentHeuristicCMP`, `DomActions`, the eval-snippet table). Self-contained -- no `import`/`require` anywhere in the file -- apart from a trailing ES-module `export { ... };` block, which is stripped at script-build time by `dv_tracking.crawl.consent_interaction` (`_strip_esm_export`); the on-disk file is byte-exact. |
| `rules.json` | 1,068,693 | `rules/rules.json` | The packaged declarative rule set consumed as `RuleBundle.autoconsent`: 1,327 CMP rules, each a `name` plus `detectCmp`/`detectPopup`/`optIn`/`optOut` step arrays. Covers `Termly`, `cookieyes`, `cookieyes-legacy`, and `wix` among this project's ten pilot sites; does not cover Elementor's cookie widget or the Enfold/Avia theme consent, a measured coverage gap rather than something patched locally. |
| `LICENSE` | 16,726 | `LICENSE` | MPL-2.0 text. |

## Reference-only

| File | Bytes | Source path in tarball | Description |
|---|---:|---|---|
| `web.d.ts` | 2,603 | `dist/types/web.d.ts` | Upstream's TypeScript declarations for the library build. Reference-quality only; not consulted at runtime. |

## What is not vendored, and why

- `dist/autoconsent.standalone.js` -- the self-executing browser bundle --
  is not used. It auto-initialises with a hardcoded config
  (`enableHeuristicDetection: true, heuristicMode: "tier2"`, no
  `autoAction`) the instant it is injected, which is wrong for a
  measurement arm that must choose `optOut`/`optIn`/neither per visit and
  must not run heuristic click guessing. `autoconsent.esm.js` is the
  library build instead: construction and config are explicit, driven by
  `dv_tracking.crawl.consent_interaction.AutoconsentInteractor`.
- `rules/compact-rules.json` and the generated/hand-maintained rule
  directories (`rules/autoconsent/*.json`, `rules/generated/*.json`) are
  not vendored separately; `rules.json` is upstream's own packaging of the
  same rules into the one `RuleBundle` shape the engine consumes.
- The CJS and Playwright dist builds (`autoconsent.cjs.js`,
  `autoconsent.playwright.js`) are not vendored; nothing in this project
  runs under CommonJS or Playwright.

## Detection side effects

`AutoconsentInteractor` sets `enablePrehide: false` (upstream's own
default is `true`, which would hide matched banner elements as a visual
side effect of merely looking for one) and `enableHeuristicDetection:
false` / `heuristicMode: "off"` (declarative rules only). Across the
1,327 vendored rules, `detectCmp`/`detectPopup` use only `exists`,
`visible`, and (for 6 rules, none of them in this project's pilot corpus)
`eval` steps -- never `click` -- confirmed by inspecting every rule's
detection-phase steps against the vendored `rules.json`. The four rules
relevant to this project's pilot sites (`Termly`, `cookieyes`,
`cookieyes-legacy`, `wix`) detect with `exists`/`visible` only.
