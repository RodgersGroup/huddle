# Huddle Upgrade Plan — Informed by AnyList Analysis

**Date**: 2026-03-01
**Source**: [AnyList Backend Report](../anylist-analysis/ANYLIST-BACKEND-REPORT.md) | [Gap Analysis](../anylist-analysis/GAP-ANALYSIS.md)

---

## Phase 1: Quick Wins (Sprint 1)

### 1.1 Security Headers Middleware

**Files**: `backend/app.py`
**Effort**: 30 minutes

Add a FastAPI middleware that sets security headers on all responses:

```python
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response
```

### 1.2 Optimistic Locking (Conflict Detection)

**Files**: `backend/db.py`, all router files that do UPDATE queries
**Effort**: 2-3 hours

**Step 1**: Add `version INTEGER DEFAULT 1` column to key tables:
- `chores`, `meals`, `calendar_events`, `shopping_items`, `bills`, `recipes`
- `household_settings`, `routines`, `pets`, `noticeboard_posts`

**Step 2**: Modify UPDATE endpoints to:
1. Accept `version` in the request body
2. Add `WHERE version = ?` to UPDATE queries
3. Increment version: `SET version = version + 1`
4. If no rows affected, return `409 Conflict` with current server state

**Step 3**: Frontend sends `version` with every edit, handles 409 by refetching.

### 1.3 WebSocket Command Pattern Refactor

**Files**: `backend/websocket.py`, all routers that call `manager.broadcast()`, `src/` (frontend)
**Effort**: 2-3 hours

**Current**: `manager.broadcast({"type": "chores_updated", "data": {full chore list}}, household_id=hid)`

**New**: `manager.broadcast({"type": "refresh", "resource": "chores"}, household_id=hid)`

**Backend changes**:
- Simplify all `broadcast()` calls to send command-only messages
- Remove data payload from WebSocket messages

**Frontend changes**:
- On receiving `{"type": "refresh", "resource": "chores"}`, call `fetchChores()` via HTTP
- This centralizes data fetching in one code path (same for initial load and real-time updates)

---

## Phase 2: Auth Hardening (Sprint 2)

### 2.1 Access/Refresh Token System

**Files**: `backend/auth.py`, `backend/db.py`
**Effort**: 4-5 hours

**New table**: `sessions`
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,           -- session UUID
    user_id INTEGER NOT NULL REFERENCES users(id),
    household_id INTEGER NOT NULL REFERENCES households(id),
    refresh_token TEXT UNIQUE NOT NULL,
    device_id TEXT,                 -- client-generated UUID
    device_name TEXT,               -- e.g., "Chrome on macOS"
    ip_address TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    last_used_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,       -- refresh token expiry (e.g., 30 days)
    revoked INTEGER DEFAULT 0
);
CREATE INDEX idx_sessions_refresh ON sessions(refresh_token) WHERE revoked = 0;
CREATE INDEX idx_sessions_user ON sessions(user_id) WHERE revoked = 0;
```

**Auth flow**:
1. Magic link login → creates session → returns `access_token` (short-lived, 15min signed JWT) + `refresh_token` (opaque, stored in `sessions` table)
2. Access token: `itsdangerous` signed payload with `{user_id, household_id, session_id}`, 15-min max_age
3. Refresh token: `secrets.token_urlsafe(64)`, stored in HttpOnly cookie
4. On 401: frontend calls `POST /api/auth/refresh` with refresh_token cookie → gets new access token
5. Refresh rotates: old refresh_token revoked, new one issued
6. Sign-out: `POST /api/auth/logout` revokes the session

**Backward compatibility**: Keep magic link flow as the login mechanism. The change is in what happens AFTER magic link verification — instead of a single session cookie, issue access + refresh tokens.

### 2.2 Multi-Device Session Management

**Files**: `backend/auth.py`, new `backend/routers/sessions.py`
**Effort**: 1-2 hours

**New endpoints**:
- `GET /api/auth/sessions` — list active sessions for current user
- `DELETE /api/auth/sessions/{session_id}` — revoke a specific session
- `DELETE /api/auth/sessions` — revoke all sessions except current (logout everywhere)

**Frontend**: Add a "Sessions" section to user settings showing active devices with "Log out" buttons.

**Headers**: Frontend sends `X-Device-ID: <uuid>` (generated once, stored in localStorage) and `User-Agent` for device name extraction.

---

## Phase 3: Sync Architecture (Sprint 3)

### 3.1 Add `updated_at` Columns

**Files**: `backend/db.py` (migration), all router files
**Effort**: 2-3 hours

Add to all data tables:
```sql
ALTER TABLE chores ADD COLUMN updated_at TEXT DEFAULT (datetime('now'));
ALTER TABLE meals ADD COLUMN updated_at TEXT DEFAULT (datetime('now'));
ALTER TABLE calendar_events ADD COLUMN updated_at TEXT DEFAULT (datetime('now'));
-- ... (all data tables)
```

Create a trigger (or handle in Python) to auto-update `updated_at` on every UPDATE:
```sql
CREATE TRIGGER chores_updated_at AFTER UPDATE ON chores
BEGIN
    UPDATE chores SET updated_at = datetime('now') WHERE id = NEW.id;
END;
```

### 3.2 Delta Sync on GET Endpoints

**Files**: All router files with GET/list endpoints
**Effort**: 3-4 hours

**Pattern**: Add optional `since` query parameter to all list endpoints:
```python
@router.get("/api/chores")
async def list_chores(tenant: TenantContext = Depends(get_current_user), since: str = None):
    db = get_db()
    if since:
        rows = db.execute(
            "SELECT * FROM chores WHERE household_id = ? AND updated_at > ?",
            (tenant.household_id, since)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM chores WHERE household_id = ?",
            (tenant.household_id,)
        ).fetchall()
    return {"chores": [dict(r) for r in rows], "server_time": datetime.utcnow().isoformat()}
```

**Response always includes `server_time`** — frontend stores this and sends it as `since` on next request.

### 3.3 Bulk Sync Endpoint

**Files**: New `backend/routers/sync.py`
**Effort**: 2-3 hours

**Endpoint**: `POST /api/sync`

**Request body**:
```json
{
  "timestamps": {
    "chores": "2026-03-01T10:00:00",
    "meals": "2026-03-01T09:30:00",
    "calendar": "2026-03-01T08:00:00",
    "shopping": null
  }
}
```

**Response**:
```json
{
  "server_time": "2026-03-01T12:00:00",
  "changes": {
    "chores": [/* only changed chores */],
    "meals": [],
    "calendar": [/* changed events */],
    "shopping": [/* all items (null timestamp = full fetch) */]
  },
  "deleted": {
    "chores": ["uuid-1", "uuid-2"],
    "meals": []
  }
}
```

This replaces multiple individual GET calls with a single round-trip, similar to AnyList's `/data/user-data/get`.

### 3.4 Soft Deletes

**Files**: `backend/db.py`, router files
**Effort**: 2 hours

To support delta sync properly, deleted items must be trackable:
```sql
ALTER TABLE chores ADD COLUMN deleted_at TEXT DEFAULT NULL;
```

Instead of `DELETE FROM chores WHERE id = ?`, use:
```sql
UPDATE chores SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?;
```

The delta sync endpoint returns deleted IDs so clients can remove them locally.

---

## Phase 4: Offline Support (Future Sprint)

### 4.1 Service Worker

**Files**: `src/sw.js` (new), `src/main.js`
**Effort**: 3-4 hours

- Cache static assets (HTML, CSS, JS, images)
- Cache API responses for offline viewing
- Show offline indicator in UI

### 4.2 IndexedDB Operation Queue

**Files**: `src/lib/syncQueue.js` (new), `src/lib/api.js`
**Effort**: 4-5 hours

- Wrap all mutation API calls (POST/PUT/DELETE) in a queue
- If online: send immediately, store in queue as backup
- If offline: persist to IndexedDB, show "pending sync" indicator
- On reconnect: replay queued operations in order

### 4.3 Sync Reconciliation

**Files**: `backend/routers/sync.py`
**Effort**: 3-4 hours

- `POST /api/sync/push` — accept batch of queued operations
- Server processes each operation, returns results (success/conflict per operation)
- Client removes confirmed operations from IndexedDB
- Conflicted operations trigger refetch of affected resources

---

## Implementation Sequence

```
Phase 1 (Quick Wins)           Phase 2 (Auth)              Phase 3 (Sync)
┌─────────────────────┐       ┌──────────────────┐        ┌─────────────────┐
│ 1.1 Security Headers│──┐    │ 2.1 Token Auth   │──┐     │ 3.1 updated_at  │
│ 1.2 Optimistic Lock │  │    │ 2.2 Multi-Device │  │     │ 3.2 Delta Sync  │
│ 1.3 WS Commands     │  │    └──────────────────┘  │     │ 3.3 Bulk Sync   │
└─────────────────────┘  │                           │     │ 3.4 Soft Deletes│
                         └───────────────────────────┘     └─────────────────┘
                                     │
                                     ▼
                              Phase 4 (Offline - Future)
                              ┌──────────────────────┐
                              │ 4.1 Service Worker   │
                              │ 4.2 IndexedDB Queue  │
                              │ 4.3 Sync Reconcile   │
                              └──────────────────────┘
```

## Files Changed Per Phase

### Phase 1
| File | Change |
|------|--------|
| `backend/app.py` | Add security headers middleware |
| `backend/db.py` | Add `version` column migration |
| `backend/websocket.py` | No change (already clean) |
| `backend/routers/*.py` | Add `version` to UPDATE queries, simplify broadcast calls |
| `src/` (frontend) | Handle 409 Conflict, fetch on WS command |

### Phase 2
| File | Change |
|------|--------|
| `backend/auth.py` | Add token generation, refresh, revocation |
| `backend/db.py` | Add `sessions` table migration |
| `backend/routers/sessions.py` | New file — session management endpoints |
| `src/` (frontend) | Token storage, auto-refresh on 401, device ID |

### Phase 3
| File | Change |
|------|--------|
| `backend/db.py` | Add `updated_at` and `deleted_at` columns, triggers |
| `backend/routers/*.py` | Add `since` parameter to GET endpoints |
| `backend/routers/sync.py` | New file — bulk sync endpoint |
| `src/` (frontend) | Track `server_time`, send `since`, handle deletes |

### Phase 4
| File | Change |
|------|--------|
| `src/sw.js` | New — Service Worker |
| `src/lib/syncQueue.js` | New — IndexedDB operation queue |
| `src/lib/api.js` | Wrap mutations in queue |
| `backend/routers/sync.py` | Add `POST /api/sync/push` |

---

## Success Criteria

### Phase 1
- [ ] All responses include HSTS, CSP, X-Content-Type-Options headers
- [ ] Concurrent edits to the same chore return 409 instead of silently overwriting
- [ ] WebSocket messages are <100 bytes (commands only, no data payloads)
- [ ] Frontend fetches data via HTTP after receiving WebSocket command

### Phase 2
- [ ] Magic link login issues access + refresh token pair
- [ ] Access token expires in 15 minutes, refresh in 30 days
- [ ] 401 response triggers automatic token refresh without user interaction
- [ ] User can view and revoke active sessions in settings

### Phase 3
- [ ] `GET /api/chores?since=<timestamp>` returns only changed items
- [ ] `POST /api/sync` fetches changes across all resources in one request
- [ ] Deleted items are soft-deleted and included in delta sync responses
- [ ] Frontend stores `server_time` and sends it on subsequent requests

### Phase 4
- [ ] App shows cached data when offline
- [ ] Edits made offline are queued and synced on reconnect
- [ ] Conflicted operations show user notification
