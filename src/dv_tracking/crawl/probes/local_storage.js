// Probe: first-party localStorage snapshot (top document origin only).
// Runs in the top frame, so only the visited origin's store is read; cross-
// origin iframe localStorage is out of scope. Invoked with: no arguments
// Returns: {entries: [[k, v], ...], error}
// Side effects: none (read-only enumeration of localStorage).

(function localStorageSnapshot() {
    let entries = [];
    let error = null;
    try {
        for (let i = 0; i < window.localStorage.length; i++) {
            const k = window.localStorage.key(i) || '';
            const v = window.localStorage.getItem(k) || '';
            entries.push([k, v]);
        }
    } catch (e) {
        error = String(e);
    }
    return {entries, error};
})()
