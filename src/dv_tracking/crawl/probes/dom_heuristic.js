// Probe: DOM heuristic for a cookie-consent banner, its close control, and a quick-exit button.
// Invoked with: {zIndexMin, keywords, excerptMax}
// Returns: {matched, evidence, close_button_present, quick_exit_present}
// Side effects: none (read-only DOM walk).

(function domHeuristic({zIndexMin, keywords, excerptMax}) {
    let matched = false;
    let evidence = null;
    let closeButtonPresent = false;
    let quickExitPresent = false;

    const kws = keywords.map(k => k.toLowerCase());
    const CLOSE_SELECTORS = [
        '[aria-label*="close" i]',
        '[aria-label*="dismiss" i]',
        '[title*="close" i]',
        '[class*="close"]',
        '[class*="dismiss"]',
        'button[data-testid*="close" i]',
    ];

    // Fixed / sticky elements above the z-index floor whose text carries a consent keyword.
    for (const ele of document.querySelectorAll('*')) {
        let cs;
        try { cs = window.getComputedStyle(ele); } catch (e) { continue; }
        if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
        const zidx = parseInt(cs.zIndex, 10);
        if (!Number.isFinite(zidx) || zidx < zIndexMin) continue;
        const txt = (ele.innerText || '').toLowerCase();
        if (!txt || !kws.some(k => txt.includes(k))) continue;
        matched = true;
        evidence = (ele.innerText || '').slice(0, excerptMax);
        closeButtonPresent = CLOSE_SELECTORS.some(sel => {
            try { return ele.querySelector(sel) !== null; }
            catch (e) { return false; }
        });
        break;
    }

    // Quick-exit / safety UI (DV-sector convention): "Exit Site / Quick Exit / Leave Site".
    const QUICK_EXIT_KWS = [
        'exit site', 'quick exit', 'leave site', 'quick leave',
        'escape site', 'safety exit', 'hide site', 'hide this site',
    ];
    for (const ele of document.querySelectorAll('a, button, [role="button"], [role="link"]')) {
        const txt = ((ele.innerText || ele.textContent || '') + '')
            .replace(/\s+/g, ' ').trim().toLowerCase();
        if (!txt) continue;
        if (QUICK_EXIT_KWS.some(k => txt.includes(k))) { quickExitPresent = true; break; }
        // Bare "exit" needs an accompanying icon to disambiguate.
        if (txt === 'exit') {
            const hasIcon = (
                ele.querySelector('i[class*="close"]') ||
                ele.querySelector('i[class*="exit"]') ||
                ele.querySelector('svg')
            );
            if (hasIcon) { quickExitPresent = true; break; }
        }
    }

    return {
        matched: matched,
        evidence: evidence,
        close_button_present: closeButtonPresent,
        quick_exit_present: quickExitPresent,
    };
})
