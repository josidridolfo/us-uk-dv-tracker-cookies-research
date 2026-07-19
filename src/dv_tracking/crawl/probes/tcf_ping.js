// Probe: IAB TCF v2 ping.
// Called:  page.evaluate(source)
// Returns: the PingReturn dict when __tcfapi is registered; null otherwise.
// Side effects: none (ping is side-effect-free per the IAB TCF v2 spec). 1500 ms timeout guard.

(function tcfPing() {
    if (typeof window.__tcfapi !== 'function') return null;
    return Promise.race([
        new Promise((r) => window.__tcfapi('ping', 2, r)),
        new Promise((r) => setTimeout(() => r(null), 1500)),
    ]);
})()
