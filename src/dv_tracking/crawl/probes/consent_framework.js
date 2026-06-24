// Probe: framework / CMP tell detection.
// Called:  page.evaluate(source, cmpIdentifiers)
// Returns: {usp_present, gpp_present, cmp_matches}
// Side effects: none (typeof-checks and a read-only script-src scan).

(function consentFramework(cmpIdentifiers) {
    const uspPresent = typeof window.__uspapi === 'function';
    const gppPresent = typeof window.__gpp === 'function';

    // Lowercased script srcs searched for each known CMP rule identifier as a substring.
    const haystacks = [];
    for (const s of document.querySelectorAll('script[src]')) {
        haystacks.push((s.getAttribute('src') || '').toLowerCase());
    }
    const cmpMatches = [];
    for (const id of (cmpIdentifiers || [])) {
        const needle = String(id).toLowerCase();
        if (needle && haystacks.some(h => h.includes(needle))) cmpMatches.push(id);
    }

    return {usp_present: uspPresent, gpp_present: gppPresent, cmp_matches: cmpMatches};
})
