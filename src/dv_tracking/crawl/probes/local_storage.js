// Probe: localStorage snapshot.
// Called:  page.evaluate(source)
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
