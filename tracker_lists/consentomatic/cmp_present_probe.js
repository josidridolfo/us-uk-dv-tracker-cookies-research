// Presence-only Consent-O-Matic probe.
//
// thin wrapper that consumes the upstream
// Consent-O-Matic `Tools` class (vendored verbatim in `Tools.js`) and
// the upstream consolidated rule set (vendored verbatim in
// `Rules.json`). The wrapper loops the `detectors[]` of every rule and
// asks `Tools.find()` whether the rule's `presentMatcher` resolves to
// an element on the current page. A rule "matches" when any of its
// detectors' present matchers returns a non-null target. Returns the
// set of matched rule keys plus an error string when iteration fails.
//
// called by Playwright `page.evaluate(...)`

function detectPresentMatchers(rules) {
    const matched = [];
    try {
        for (const [key, rule] of Object.entries(rules)) {
            const detectors = (rule && rule.detectors) || [];
            let hit = false;
            for (const detector of detectors) {
                const pm = detector && detector.presentMatcher;
                if (pm == null) continue;
                const candidates = Array.isArray(pm) ? pm : [pm];
                for (const candidate of candidates) {
                    if (!candidate || !candidate.target) continue;
                    try {
                        const result = Tools.find(candidate);
                        if (result && result.target != null) {
                            hit = true;
                            break;
                        }
                    } catch (_) {
                        // Malformed selector or filter; skip this candidate.
                    }
                }
                if (hit) break;
            }
            if (hit) matched.push(key);
        }
        return { matched: matched, error: null };
    } catch (e) {
        return { matched: matched, error: String(e) };
    }
}
