// Service Worker for Huddle PWA - v14
const STATIC_CACHE = 'huddle-static-v14';
const API_CACHE = 'huddle-api-v14';

// Assets to precache on install
const PRECACHE_URLS = [
    '/mobile',
    '/static/icon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/badge-96.png',
    '/manifest.json'
];

// API paths to cache with stale-while-revalidate
const CACHEABLE_API_PATHS = [
    '/api/chores',
    '/api/adhoc',
    '/api/meals',
    '/api/fuel',
    '/api/weather',
    '/api/quote'
];

// Install: precache static assets
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then(cache => {
            return cache.addAll(PRECACHE_URLS);
        }).then(() => self.skipWaiting())
    );
});

// Activate: clean old caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(names => {
            return Promise.all(
                names.filter(name =>
                    name !== STATIC_CACHE && name !== API_CACHE
                ).map(name => caches.delete(name))
            );
        }).then(() => clients.claim())
    );
});

// Fetch handler with strategy routing
self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;

    const url = new URL(event.request.url);

    // Cacheable API requests: stale-while-revalidate
    if (url.pathname.startsWith('/api/') && CACHEABLE_API_PATHS.some(p => url.pathname.startsWith(p))) {
        event.respondWith(staleWhileRevalidate(event.request, API_CACHE));
        return;
    }

    // Calendar API: network-first (changes frequently)
    if (url.pathname.startsWith('/api/family-calendar/')) {
        event.respondWith(networkFirst(event.request, API_CACHE));
        return;
    }

    // Other API paths: network only
    if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;

    // Kiosk pages: always bypass service worker (token embedded in HTML)
    if (url.pathname === '/' || url.pathname === '/kiosk') return;

    // Static assets: cache-first
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(cacheFirst(event.request, STATIC_CACHE));
        return;
    }

    // HTML pages: network-first
    event.respondWith(networkFirst(event.request, STATIC_CACHE));
});

// Cache-first: static assets that rarely change
async function cacheFirst(request, cacheName) {
    const cached = await caches.match(request);
    if (cached) return cached;
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
        }
        return response;
    } catch {
        return new Response('Offline', { status: 503 });
    }
}

// Network-first: HTML pages, frequently changing data
async function networkFirst(request, cacheName) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
        }
        return response;
    } catch {
        const cached = await caches.match(request);
        if (cached) return cached;
        if (request.headers.get('accept')?.includes('text/html')) {
            const fallback = await caches.match('/mobile');
            if (fallback) return fallback;
        }
        return new Response('Offline', { status: 503 });
    }
}

// Stale-while-revalidate: return cache immediately, update in background
async function staleWhileRevalidate(request, cacheName) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(request);

    const fetchPromise = fetch(request).then(response => {
        if (response.ok) {
            cache.put(request, response.clone());
        }
        return response;
    }).catch(() => null);

    return cached || await fetchPromise || new Response(
        JSON.stringify({ error: 'offline', cached: false }),
        { status: 503, headers: { 'Content-Type': 'application/json' } }
    );
}

// Re-register push subscription with the server to keep it fresh
async function refreshPushSubscription() {
    try {
        const person = await getPushPerson();
        if (!person) return;
        const subscription = await self.registration.pushManager.getSubscription();
        if (!subscription) return;
        await fetch('/api/push/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ person: person, subscription: subscription.toJSON() })
        });
    } catch (e) {
        // Non-critical — subscription will refresh next time user opens the app
    }
}

// Store/retrieve the person name for push re-registration
async function setPushPerson(name) {
    try {
        const cache = await caches.open('huddle-push-meta');
        await cache.put('/_push_person', new Response(name));
    } catch (e) {}
}

async function getPushPerson() {
    try {
        const cache = await caches.open('huddle-push-meta');
        const resp = await cache.match('/_push_person');
        return resp ? await resp.text() : null;
    } catch (e) {
        return null;
    }
}

// Listen for messages from the main app
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'set_push_person') {
        setPushPerson(event.data.person);
    }
    // Register Background Sync when offline mutations are queued
    if (event.data && event.data.type === 'register_sync') {
        if (self.registration.sync) {
            self.registration.sync.register('huddle-sync-queue').catch(() => {});
        }
    }
});

// Background Sync — replay offline queue when connectivity returns
const OFFLINE_DB_NAME = 'huddle-offline';
const OFFLINE_STORE = 'queue';

self.addEventListener('sync', (event) => {
    if (event.tag === 'huddle-sync-queue') {
        event.waitUntil(replayOfflineQueue());
    }
});

async function replayOfflineQueue() {
    try {
        const db = await openOfflineDB();
        const ops = await getAllFromStore(db);
        if (!ops.length) return;

        const payload = {
            operations: ops.map(op => ({
                id: op.id, url: op.url, method: op.method,
                body: op.body, tempId: op.tempId, timestamp: op.timestamp
            }))
        };

        const resp = await fetch('/api/sync/push', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(payload)
        });

        if (!resp.ok) throw new Error('Sync push failed: ' + resp.status);
        const data = await resp.json();

        // Remove completed/conflicted ops from IndexedDB
        for (const r of (data.results || [])) {
            if (r.status === 'ok' || r.status === 'conflict') {
                await deleteFromStore(db, r.id);
            }
        }

        // Notify main page
        const allClients = await clients.matchAll({ type: 'window' });
        for (const client of allClients) {
            client.postMessage({ type: 'sync_complete', results: data.results });
        }
    } catch (e) {
        // Will retry on next sync event
    }
}

function openOfflineDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(OFFLINE_DB_NAME, 1);
        req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains(OFFLINE_STORE)) {
                db.createObjectStore(OFFLINE_STORE, { keyPath: 'id', autoIncrement: true });
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

function getAllFromStore(db) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(OFFLINE_STORE, 'readonly');
        const req = tx.objectStore(OFFLINE_STORE).getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
    });
}

function deleteFromStore(db, id) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(OFFLINE_STORE, 'readwrite');
        const req = tx.objectStore(OFFLINE_STORE).delete(id);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
    });
}

// Push notification handler — branded Huddle notifications
self.addEventListener('push', (event) => {
    let data = { title: 'Huddle', body: 'You have something to check!' };
    try {
        data = event.data.json();
    } catch (e) {
        data.body = event.data ? event.data.text() : data.body;
    }

    const options = {
        body: data.body || 'You have something to check!',
        icon: '/static/icon-192.png',
        badge: '/static/badge-96.png',
        tag: data.tag || 'huddle-notification',
        renotify: data.renotify || false,
        vibrate: [100, 50, 100],
        data: data
    };

    // Add notification actions if provided by the server
    if (data.actions && Array.isArray(data.actions)) {
        options.actions = data.actions;
    }

    event.waitUntil(
        Promise.all([
            self.registration.showNotification(data.title || 'Huddle', options),
            refreshPushSubscription()
        ])
    );
});

// Notification click — open the app and deep-link to relevant module
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const notifData = event.notification.data || {};
    const module = notifData.module || '';
    const action = event.action;

    // Handle action button clicks
    if (action === 'dismiss') {
        return; // Just close the notification
    }

    const targetUrl = module ? `/mobile?module=${module}` : '/mobile';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            for (const client of clientList) {
                if (client.url.includes('/mobile') && 'focus' in client) {
                    if (module) {
                        client.postMessage({ type: 'open_module', module: module });
                    }
                    return client.focus();
                }
            }
            return clients.openWindow(targetUrl);
        })
    );
});
