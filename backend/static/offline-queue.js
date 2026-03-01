/**
 * Huddle Offline Queue — IndexedDB-backed mutation queue with batch replay.
 *
 * When a mutation (POST/PUT/DELETE) to /api/* fails due to a network error,
 * the fetch wrapper in mobile.html enqueues it here. On reconnect (or page
 * load while online), replay() sends everything to /api/sync/push as a batch.
 */
(function () {
    'use strict';

    const DB_NAME = 'huddle-offline';
    const DB_VERSION = 1;
    const STORE_NAME = 'queue';

    let _db = null;

    function open() {
        if (_db) return Promise.resolve(_db);
        return new Promise(function (resolve, reject) {
            const req = indexedDB.open(DB_NAME, DB_VERSION);
            req.onupgradeneeded = function (e) {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                }
            };
            req.onsuccess = function (e) {
                _db = e.target.result;
                resolve(_db);
            };
            req.onerror = function () {
                reject(req.error);
            };
        });
    }

    function enqueue(url, method, headers, body) {
        return open().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var entry = {
                    url: url,
                    method: method,
                    headers: headers || {},
                    body: body || null,
                    tempId: 'temp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
                    timestamp: new Date().toISOString(),
                    retries: 0
                };
                var req = store.add(entry);
                req.onsuccess = function () {
                    entry.id = req.result;
                    resolve(entry);
                };
                req.onerror = function () { reject(req.error); };
                tx.oncomplete = function () {
                    updateBadge();
                    // Ask service worker to register Background Sync
                    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
                        navigator.serviceWorker.controller.postMessage({ type: 'register_sync' });
                    }
                };
            });
        });
    }

    function dequeue(id) {
        return open().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readwrite');
                var req = tx.objectStore(STORE_NAME).delete(id);
                req.onsuccess = function () { resolve(); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function getAll() {
        return open().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readonly');
                var req = tx.objectStore(STORE_NAME).getAll();
                req.onsuccess = function () { resolve(req.result || []); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function count() {
        return open().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readonly');
                var req = tx.objectStore(STORE_NAME).count();
                req.onsuccess = function () { resolve(req.result); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function replay() {
        return getAll().then(function (ops) {
            if (!ops.length) {
                updateBadge();
                return { synced: 0 };
            }
            // Use _origFetch to bypass our wrapper (avoids re-queuing on failure)
            var fetchFn = window._origFetch || window.fetch;
            var payload = {
                operations: ops.map(function (op) {
                    return {
                        id: op.id,
                        url: op.url,
                        method: op.method,
                        body: op.body,
                        tempId: op.tempId,
                        timestamp: op.timestamp
                    };
                })
            };
            return fetchFn.call(window, '/api/sync/push', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify(payload)
            }).then(function (resp) {
                if (!resp.ok) throw new Error('Sync push failed: ' + resp.status);
                return resp.json();
            }).then(function (data) {
                var results = data.results || [];
                var removals = [];
                results.forEach(function (r) {
                    // Remove successful and conflicted ops (server-wins, no point retrying)
                    if (r.status === 'ok' || r.status === 'conflict') {
                        removals.push(dequeue(r.id));
                    }
                    // 'error' ops stay in queue for retry
                });
                return Promise.all(removals).then(function () {
                    updateBadge();
                    var synced = results.filter(function (r) { return r.status === 'ok'; }).length;
                    var conflicts = results.filter(function (r) { return r.status === 'conflict'; }).length;
                    return { synced: synced, conflicts: conflicts, errors: results.length - synced - conflicts };
                });
            }).catch(function (err) {
                console.warn('[offline-queue] Replay failed:', err.message);
                updateBadge();
                return { synced: 0, error: err.message };
            });
        });
    }

    function updateBadge() {
        count().then(function (n) {
            var badge = document.getElementById('pendingSyncBadge');
            if (!badge) return;
            if (n > 0) {
                badge.textContent = n + ' pending';
                badge.style.display = 'flex';
            } else {
                badge.style.display = 'none';
            }
        }).catch(function () { /* ignore */ });
    }

    window.offlineQueue = {
        open: open,
        enqueue: enqueue,
        dequeue: dequeue,
        getAll: getAll,
        count: count,
        replay: replay,
        updateBadge: updateBadge
    };
})();
