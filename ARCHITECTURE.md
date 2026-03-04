# Huddle — Architecture Document

> **Last updated:** 2026-03-03
> **Status:** Active development (beta)
> **Domain:** huddle.rodgersgroup.au

Huddle is a household management platform supporting families, solo dwellers, and sharehouses. It provides chore scheduling, meal planning, shopping lists, calendars, bills, expenses, and 20+ other modules — all behind a tiered subscription model (currently in free beta).

---

## Summary

Huddle is a **single-server monorepo** running on Ubuntu with a **FastAPI** backend (Python 3.12) and a **React 19 + Vite 6** frontend styled with Tailwind v4. All data lives in **SQLite** (WAL mode) with ~40 tables across 42 forward-only migrations — no ORM, just raw SQL.

**Backend:** 30 API routers cover 20+ feature modules (chores, meals, shopping, calendar, bills, pets, vehicles, expenses, selfcare, and more). Background async tasks handle a systemd watchdog, push notifications, and a self-healing AI agent system that scans for issues every 5 minutes.

**Frontend:** Two frontends coexist — production-active **Jinja2 server-rendered pages** (`/kiosk`, `/mobile`) and an in-progress **React SPA** (`src/`) that hasn't yet wired up to the backend API.

**Auth:** Session cookies (signed with `itsdangerous`) for browsers, bearer tokens for mobile/API clients, kiosk tokens for shared displays, and magic link email login. Multi-tenant — every request is scoped to a `household_id` via a `TenantContext` dependency.

**Sync:** Delta sync engine (client sends per-resource timestamps, server returns only changed items) plus offline mutation replay. WebSocket broadcasts for real-time kiosk updates.

**AI:** Local Ollama LLM for shopping categorization, duplicate detection, and recipe suggestions — with keyword-based fallbacks.

**Deployment:** Single uvicorn process managed by systemd (`WatchdogSec=60`), accessed via Cloudflare Tunnel (internet) or direct LAN (kiosk). Cron jobs handle fuel price refresh, DB maintenance, tunnel health, and periodic restarts.

**Monetization:** Tiered pricing (Free → Home+ at $14.99/mo) with segment-based module gating (solo, household, sharehouse). Currently all limits are bypassed (`BETA_MODE = True`).

---

## Table of Contents

1. [High-Level Overview](#1-high-level-overview)
2. [System Architecture](#2-system-architecture)
3. [Project Structure](#3-project-structure)
4. [Backend (FastAPI)](#4-backend-fastapi)
5. [Frontend (Vite + React)](#5-frontend-vite--react)
6. [Database (SQLite)](#6-database-sqlite)
7. [Authentication & Authorization](#7-authentication--authorization)
8. [API Design](#8-api-design)
9. [Real-Time & Sync](#9-real-time--sync)
10. [Module & Tier System](#10-module--tier-system)
11. [AI Integration](#11-ai-integration)
12. [Self-Healing Agents](#12-self-healing-agents)
13. [Notifications](#13-notifications)
14. [Deployment & Operations](#14-deployment--operations)
15. [Configuration](#15-configuration)
16. [Key Design Decisions](#16-key-design-decisions)

---

## 1. High-Level Overview

```
┌─────────────────────────────────────────────────────────┐
│                      Clients                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Mobile   │  │ Desktop  │  │ Kiosk (Raspberry Pi) │  │
│  │ PWA      │  │ Browser  │  │ LAN Display          │  │
│  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘  │
└───────┼──────────────┼───────────────────┼──────────────┘
        │              │                   │
        ▼              ▼                   ▼
┌───────────────────────────────────────────────────────┐
│  Cloudflare Tunnel (remote)  /  LAN direct (:8001)   │
└───────────────────────┬───────────────────────────────┘
                        ▼
┌───────────────────────────────────────────────────────┐
│              FastAPI (uvicorn, port 8001)             │
│  ┌────────────────────────────────────────────────┐   │
│  │  Middleware: CORS · GZip · CSRF · Security     │   │
│  │  Headers · Request ID · Page Views             │   │
│  ├────────────────────────────────────────────────┤   │
│  │  30 API Routers (/api/*)                       │   │
│  │  Jinja2 Pages (/, /kiosk, /mobile, /pair)      │   │
│  │  Static Files (dist/ → /static)                │   │
│  │  WebSocket (/ws)                               │   │
│  ├────────────────────────────────────────────────┤   │
│  │  Background Tasks: Watchdog · Push · Agents    │   │
│  └────────────────────┬───────────────────────────┘   │
│                       │                               │
│  ┌────────────────────▼───────────────────────────┐   │
│  │  SQLite (WAL mode) — chores.db                 │   │
│  │  42 migrations · ~40 tables                    │   │
│  └────────────────────────────────────────────────┘   │
│                                                       │
│  ┌────────────────────────────────────────────────┐   │
│  │  Ollama (localhost:11434) — local LLM           │   │
│  └────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
```

---

## 2. System Architecture

### Traffic Flow

```
Internet → Cloudflare Tunnel (remote-managed) → FastAPI :8001
Local LAN → Direct to :8001 → Kiosk mode auto-detected
```

There is **no reverse proxy** (nginx) between the tunnel and FastAPI for Huddle — the tunnel routes directly to the application. LAN clients (e.g. a Raspberry Pi kiosk) access port 8001 directly.

### Process Model

A single uvicorn process handles:
- HTTP API requests
- WebSocket connections
- Jinja2-rendered pages
- Static file serving (the built React SPA)
- Background async tasks (watchdog, push notifications, self-healing agents)

systemd manages the process with `WatchdogSec=60` — the background task loop pings systemd's watchdog via `sdnotify` each cycle, and systemd restarts the process if a ping is missed.

---

## 3. Project Structure

```
huddle/
├── CLAUDE.md                     # AI assistant instructions
├── ARCHITECTURE.md               # This file
├── package.json                  # Frontend deps (React 19, Vite 6, Tailwind v4)
├── vite.config.js                # Dev proxy → backend
│
├── src/                          # ── React Frontend ──
│   ├── main.jsx                  # Entry point
│   ├── index.css                 # Global styles
│   ├── app/
│   │   └── HuddleApp.jsx        # Root component (auth gate + routing)
│   ├── components/               # Shared UI components
│   ├── context/                  # React contexts (Auth, Data, Theme)
│   ├── hooks/                    # Custom hooks per feature
│   ├── screens/                  # Screen-level components
│   ├── data/                     # Seed data (local dev)
│   └── theme/                    # Theme definitions
│
├── dist/                         # Vite build output (served by FastAPI)
│
├── backend/                      # ── FastAPI Backend ──
│   ├── app.py                    # Entrypoint (~400 lines)
│   ├── auth.py                   # Auth system (sessions, bcrypt, rate limiting)
│   ├── db.py                     # SQLite connection pool + migration runner
│   ├── config.py                 # Environment-aware configuration
│   ├── settings.py               # Household settings helpers
│   ├── tiers.py                  # Tier & segment definitions
│   ├── background.py             # Async background tasks
│   ├── websocket.py              # WebSocket connection manager
│   ├── push.py                   # VAPID web push
│   ├── fuel.py                   # NSW fuel price router
│   ├── fuel_app.py               # Cron script for fuel refresh
│   ├── ollama_helper.py          # LLM integration
│   ├── logging_config.py         # Rotating file + structured logging
│   ├── page_views.py             # In-memory analytics
│   │
│   ├── routers/                  # 30 feature routers
│   │   ├── chores.py
│   │   ├── calendar.py
│   │   ├── meals.py
│   │   ├── shopping.py
│   │   ├── bills.py
│   │   ├── expenses.py
│   │   ├── sync.py               # Delta sync engine
│   │   └── ...                   # (27 more)
│   │
│   ├── migrations/               # 42 numbered migration files
│   ├── agents/                   # Self-healing AI agent system
│   ├── helpers/                  # Shared utilities (ollama, response)
│   ├── logs/                     # Rotating log files
│   ├── chores.db                 # Primary SQLite database
│   ├── .env                      # Environment variables
│   └── huddle.service            # systemd unit file
```

---

## 4. Backend (FastAPI)

### Entrypoint (`app.py`)

The FastAPI app is configured with:

**Middleware stack** (applied in order):
1. **CORS** — `https://huddle.rodgersgroup.au` in production, `*` in dev
2. **GZip** — responses > 1KB
3. **Request ID** — UUID added as `X-Request-ID` header for log correlation
4. **CSRF check** — all POST/PUT/DELETE/PATCH to `/api/*` require `X-Requested-With: XMLHttpRequest` (exemptions for auth, signup, webhooks)
5. **Security headers** — `X-Content-Type-Options`, `X-Frame-Options: DENY`, HSTS, CSP
6. **Static cache** — `Cache-Control: public, max-age=86400` for `/static/*`
7. **Page view tracker** — records path visits on success

**Exception handlers:**
- `sqlite3.Error` → HTTP 500 with `{"detail": "Database error"}`
- Generic `Exception` → HTTP 500 with `{"detail": "Internal server error"}`

**Background tasks** (started on app startup):
- **Watchdog loop** — pings systemd, runs GC, cleans pairing codes, flushes page views
- **Service monitor** — health checks
- **Push dispatcher** — periodic notification sends
- **Agent loop** — self-healing agents every 5 minutes (starts after 60s delay)

### Routers (30 modules)

| Router | Prefix | Description |
|--------|--------|-------------|
| `chores` | `/api/chores` | Recurring chore CRUD, completions, rotation |
| `calendar` | `/api/calendar` | Events with recurrence (RRULE) + exceptions |
| `meals` | `/api/meals` | Weekly planner, cook duty rotation |
| `shopping` | `/api/shopping` | Shopping list, AI categorization |
| `bills` | `/api/bills` | Bill tracking with recurrence |
| `inventory` | `/api/inventory` | Pantry with low-stock alerts |
| `recipes` | `/api/recipes` | Recipe book, ingredient → shopping list |
| `adhoc` | `/api/adhoc` | One-off tasks |
| `routines` | `/api/routines` | Morning/evening checklists with streaks |
| `rewards` | `/api/rewards` | Points system + redemptions |
| `allowances` | `/api/allowances` | Pocket money + savings goals |
| `pets` | `/api/pets` | Pet profiles, tasks, vet appointments |
| `assignments` | `/api/assignments` | Homework tracking |
| `noticeboard` | `/api/noticeboard` | Household announcements |
| `expenses` | `/api/expenses` | Sharehouse expense splitting |
| `house_rules` | `/api/house_rules` | Shared agreements |
| `tenancy` | `/api/tenancy` | Rental/lease info |
| `freezer` | `/api/freezer` | Freezer inventory |
| `selfcare` | `/api/selfcare` | Medication/care tracking + stock |
| `polls` | `/api/polls` | Decision polls |
| `settings_api` | `/api/settings` | Household settings |
| `modules` | `/api/modules` | Enabled modules per household |
| `sync` | `/api/sync` | Delta sync engine |
| `sessions` | `/api/auth/sessions` | Multi-device session management |
| `status` | `/api/status` | System status |
| `pairing` | `/api/pair` | Kiosk QR pairing |
| `feedback` | `/api/feedback` | Bug reports / suggestions |
| `client_errors` | `/api/client-errors` | Frontend JS error logs |
| `agents` | `/api/agents` | Agent audit trail + health |
| `fuel` | `/api/fuel` | NSW fuel prices |

### Page Routes

| Path | Description |
|------|-------------|
| `GET /` | Kiosk home (LAN) or landing page (external) |
| `GET /kiosk?token=...` | Kiosk display (requires valid token) |
| `GET /mobile` | Mobile PWA (redirects to `/onboard` if unauthenticated) |
| `GET /pair` | Phone-side kiosk pairing page |
| `GET /manifest.json` | PWA manifest |
| `GET /api/health` | Full health check (DB + timestamp) |
| `GET /api/health/ping` | Liveness probe (HEAD supported) |

---

## 5. Frontend (Vite + React)

### Stack
- **React 19** with JSX
- **Vite 6** for dev server and builds
- **Tailwind CSS v4** for styling
- No router library — screen switching via `useState` + transition animation
- No state management library — `useReducer` + `localStorage`

### Component Architecture

```
ThemeProvider
  └── AuthProvider (fetches /api/auth/me)
        └── AuthGate (redirects to /onboard if unauthenticated)
              └── DataProvider (useReducer + localStorage)
                    └── AppShell
                          ├── ScreenHeader
                          ├── Screen (switched by state)
                          │   ├── HomeScreen (dashboard grid)
                          │   ├── ChoresScreen
                          │   ├── CalendarScreen
                          │   ├── ShoppingScreen
                          │   ├── MealsScreen
                          │   └── PlaceholderScreen (bills, tasks)
                          ├── AddButton (FAB)
                          └── BottomNav (Home/Chores/Calendar/Meals/Shopping)
```

### State Contexts

| Context | Purpose |
|---------|---------|
| `AuthContext` | User session state, login/logout, `refreshUser()` |
| `DataContext` | App data via `useReducer` with localStorage persistence |
| `ThemeContext` | Dark/light mode, provides semantic color tokens |

### Development Maturity

> **Note:** The React frontend currently uses **local seed data** and has not yet been wired to the full backend API. The backend is significantly more feature-complete. The Jinja2-rendered pages (`/kiosk`, `/mobile`) are the production-active frontend.

### Dev Proxy (`vite.config.js`)

```
/api  →  http://127.0.0.1:8001
/auth →  http://127.0.0.1:8001
/ws   →  ws://127.0.0.1:8001
```

---

## 6. Database (SQLite)

### Configuration

| Setting | Value | Purpose |
|---------|-------|---------|
| File | `backend/chores.db` | Primary database |
| Journal | WAL | Concurrent reads during writes |
| `busy_timeout` | 5000ms | Wait before SQLITE_BUSY |
| `synchronous` | NORMAL | Balance durability vs speed |
| `cache_size` | 8MB | In-memory page cache |
| `mmap_size` | 128MB | Memory-mapped I/O |

### Connection Management (`db.py`)

- **Thread-local pool** — one connection per thread, reused across requests
- **Context manager** — commits on clean exit, rolls back on exception, does NOT close connection
- **Optimistic locking** — `check_version()` returns HTTP 409 on version conflict

### Migrations

- 42 numbered Python files in `backend/migrations/`
- Forward-only (no rollback — restore from backup instead)
- Tracked in `_migrations` table
- Run automatically at startup via `init_db()` → `run_migrations(conn)`

### Table Inventory (~40 tables)

**Core (migration 001):**
`chores`, `completions`, `meals`, `adhoc_tasks`, `calendar_events`, `calendar_event_exceptions`, `settings`, `push_subscriptions`, `bills`, `inventory_items`, `polls`, `poll_options`, `poll_votes`, `household_devices`, `users`, `households`, `household_members`, `magic_links`, `kiosk_tokens`, `household_modules`

**Extended (later migrations):**

| Table(s) | Migration | Feature |
|----------|-----------|---------|
| `shopping_items` | 003 | Shopping list |
| `recipes`, `recipe_ingredients` | 007 | Recipe book |
| `kiosk_pairing_codes` | 008 | QR kiosk pairing |
| `routines`, `routine_items`, `routine_completions` | 009 | Daily routines |
| `reward_points`, `rewards`, `reward_redemptions` | 009 | Rewards system |
| `allowance_settings`, `allowance_transactions`, `savings_goals` | 009 | Allowances |
| `pets`, `pet_tasks`, `pet_task_completions`, `pet_vet_appointments` | 009 | Pet care |
| `noticeboard_posts` | 010 | Announcements |
| `dependent_pins`, `dependent_permissions` | 012, 014 | Dependent auth |
| `assignments` | 013 | Homework |
| `expenses`, `expense_splits`, `settlements`, `shared_costs` | 015 | Sharehouse expenses |
| `house_rules`, `tenancy_info` | 015 | Sharehouse rules |
| `vehicles`, `vehicle_maintenance`, `vehicle_service_intervals` | 018, 026 | Vehicles |
| `page_views` | 019 | Analytics |
| `freezer_items` | 020 | Freezer inventory |
| `selfcare_items`, `selfcare_logs`, `selfcare_stock` | 023, 025 | Self-care |
| `meal_duties`, `meal_duty_overrides` | 027, 031 | Cook duty rotation |
| `task_photos` | 028 | Photo attachments |
| `ev_charging_sessions` | 029 | EV charging |
| `agent_actions` | 036 | Agent audit trail |
| `sessions` | 038 | Multi-device sessions |
| `client_error_logs` | 042 | Frontend error logs |

---

## 7. Authentication & Authorization

### Auth Paths

**1. Session Cookie (primary — browser users)**
- Login via email + password → signed cookie (`huddle_session`)
- Token: `itsdangerous.URLSafeTimedSerializer`, signed with `SECRET_KEY`, 30-day max age
- Cookie flags: `HttpOnly`, `SameSite=Lax`, `Secure` in production

**2. Bearer Token (mobile/kiosk API clients)**
- Access token (15-min TTL) + refresh token (30-day TTL)
- Stored in `sessions` table

**3. Kiosk Token**
- Long-lived bearer token for shared display devices
- Stored in `kiosk_tokens` table

**4. Magic Link (passwordless)**
- Email-based login via Resend API
- Tokens stored in `magic_links` table

### Auth Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/signup` | POST | Create account |
| `/api/auth/login` | POST | Login (email + password) |
| `/api/auth/logout` | POST | Clear session |
| `/api/auth/me` | GET | Current user info + renew cookie |
| `/api/auth/sessions` | GET | List active sessions |

### Multi-Tenancy

Every authenticated request resolves to a `TenantContext`:

```python
@dataclass
class TenantContext:
    user_id: int
    household_id: int
    display_name: str
    role: str                          # 'manager' | 'member'
    original_household_id: int = None  # superadmin impersonation
```

Injected via `Depends(get_current_user)`. All queries are scoped to `household_id`.

### Roles
- **manager** — full access to settings, member management, approvals
- **member** — standard access to features
- Enforced via `require_role("manager")` FastAPI dependency

### Rate Limiting
- In-memory per-IP limiter (15-minute sliding window)
- 5 auth attempts per 10 minutes (configurable)
- Resets on service restart

### Session Revocation
- `auth_version.txt` / `AUTH_VERSION` env var — bumping invalidates all tokens and DB sessions
- `force_reauth_all()` for emergency revocation

---

## 8. API Design

### Conventions

- All endpoints under `/api/...`
- CSRF: state-changing requests require `X-Requested-With: XMLHttpRequest`
- Auth: session cookies sent automatically; bearer tokens in `Authorization` header
- Response format: JSON

### CRUD Pattern

```
GET    /api/{module}              # List (scoped to household)
POST   /api/{module}              # Create
GET    /api/{module}/{id}         # Read one
PUT    /api/{module}/{id}         # Update
DELETE /api/{module}/{id}         # Delete
POST   /api/{module}/{id}/complete  # Domain action (where applicable)
```

### Optimistic Locking

Updates include a `version` field. If the version in the request doesn't match the database, the server returns **HTTP 409 Conflict**. The client should refetch and retry.

---

## 9. Real-Time & Sync

### WebSocket (`/ws`)

- Managed by `ConnectionManager` in `websocket.py`
- Broadcasts mutations to all connected clients for the same household
- Used by kiosk displays for live updates

### Delta Sync (`/api/sync`)

Inspired by AnyList's sync protocol:

```
POST /api/sync
{
  "timestamps": {
    "chores": "2024-01-01T00:00:00",
    "meals": "2024-01-01T00:00:00",
    ...
  }
}
→ Returns only items changed since each timestamp
```

**Supported resources:** chores, meals, adhoc_tasks, calendar_events, bills, shopping_items, inventory_items, recipes, noticeboard_posts, pets, routines, house_rules, selfcare_items, freezer_items, polls, rewards, assignments, expenses, settlements, shared_costs, vehicles, vehicle_maintenance, savings_goals

### Offline Push (`/api/sync/push`)

Replays a batch of mutations queued while the client was offline:
```
POST /api/sync/push
{ "mutations": [ { "resource": "chores", "action": "create", "data": {...} }, ... ] }
```

---

## 10. Module & Tier System

### Segments (household type)

| Segment | Target |
|---------|--------|
| `solo` | Solo & couples |
| `household` | Families / multi-generational |
| `sharehouse` | Flatmates / students |

### Pricing Tiers

| Tier | Monthly | Annual |
|------|---------|--------|
| Free | $0 | $0 |
| Solo+ | $2.99 | $26 |
| Duo+ | $4.99 | $44 |
| Home | $7.99 | $69 |
| Home+ | $14.99 | $129 |
| Share | $4.99 | $44 |
| Share+ | $9.99 | $89 |

> **Currently:** `BETA_MODE = True` — all tier limits are unenforced.

### Module Availability

| Category | Modules |
|----------|---------|
| **Core (free, all segments)** | chores, calendar, adhoc_tasks, weather, shopping (15-item limit on free), feedback, selfcare |
| **Paid (all segments)** | bills, inventory, polls, fuel, vehicles, freezer, noticeboard |
| **Household (paid)** | meals, recipes, routines, rewards, allowances, pets, assignments |
| **Sharehouse** | expenses, house_rules, tenancy |
| **Beta** | Controlled via `module_beta` table, superadmin access |

Modules are toggled per household in the `household_modules` table and checked at the router level.

---

## 11. AI Integration

### Ollama (Local LLM)

| Setting | Value |
|---------|-------|
| URL | `http://localhost:11434/api/generate` |
| Model | `qwen2.5-coder:7b` (shopping) / `llama3.1:8b` (general) |

**Use cases:**
1. **Shopping categorization** — Classifies items into grocery categories
2. **Duplicate detection** — Identifies duplicates/typos when adding recipe ingredients to shopping list
3. **Recipe suggestions** — Generates recipe ideas via `ollama_helper.py`

All LLM calls include fallback to keyword-based heuristics if Ollama is unavailable.

---

## 12. Self-Healing Agents

Located in `backend/agents/`:

| Agent | File | Purpose |
|-------|------|---------|
| Base | `base.py` | Shared: throttle (`should_run`), audit (`log_action`), alerting (`alert_managers`) |
| Error Healer | `error_healer.py` | Scans for system-level issues, attempts auto-fix |
| Preventive Scanner | `preventive_scanner.py` | Per-household checks (stale data, config issues) |
| Loop | `loop.py` | Async runner: 60s delay on boot, then every 5 minutes |

**Observability:**
- All actions logged to `agent_actions` table
- `GET /api/agents/actions` — audit trail
- `GET /api/agents/health` — current health summary
- Manager push notifications on agent actions

---

## 13. Notifications

### Web Push (VAPID)

- Library: `pywebpush`
- Keys: `backend/vapid_keys.json`
- Subscriptions: `push_subscriptions` table
- Triggers: chore reminders, overdue alerts, agent actions, shopping updates
- Endpoints: `POST /api/push/subscribe`, `GET /api/push/vapid-key`

### ntfy.sh

- Used for operator-level alerts (feedback submissions, critical errors)
- Configurable via `NTFY_TOPIC`, `NTFY_SERVER`, `NTFY_TOKEN` env vars

---

## 14. Deployment & Operations

### systemd Service

```ini
[Service]
Type=simple
User=keiran
WorkingDirectory=/home/keiran/huddle/backend
ExecStart=.../venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
WatchdogSec=60
Environment=ENVIRONMENT=production
```

Management:
```bash
sudo systemctl status huddle
sudo systemctl restart huddle
journalctl -u huddle -f    # live logs
```

### Cron Jobs

| Schedule | Script | Purpose |
|----------|--------|---------|
| Hourly | `fuel_app.py` | Refresh NSW fuel price cache |
| Daily 7pm | `scripts/error_autofix.sh` | Nightly error auto-fix scan |
| Weekly Sun 4pm | `scripts/weekly_maintenance.py` | DB maintenance |
| Every 15 min | `scripts/tunnel_health.py` | Cloudflare tunnel health |
| Every 3 days 2am | `systemctl restart huddle` | Periodic restart (memory hygiene) |

### Logging

- **File:** `backend/logs/huddle.log` (rotating, 3 backups)
- **Correlation:** `X-Request-ID` header + `request_id_ctx` (Python `contextvars`)
- **Client errors:** `POST /api/client-errors` → `client_error_logs` table
- **Sentry:** Optional, 10% trace sample rate

### Database Backup

SQLite in WAL mode — can be safely copied while the app is running. Backup strategy is managed at the infrastructure level (see `~/scripts/`).

---

## 15. Configuration

Configuration is loaded from environment variables with per-environment defaults (`config.py`).

| Variable | Default (dev) | Purpose |
|----------|---------------|---------|
| `SECRET_KEY` | insecure default | Session token signing (raises in prod if default) |
| `ENVIRONMENT` | `development` | Controls CORS, logging, Sentry |
| `RESEND_API_KEY` | — | Magic link email sending |
| `FUEL_API_KEY` | — | NSW FuelCheck API |
| `SESSION_MAX_AGE` | 2592000 (30d) | Cookie expiry |
| `ACCESS_TOKEN_MAX_AGE` | 900 (15m) | Bearer access token TTL |
| `REFRESH_TOKEN_MAX_AGE` | 2592000 (30d) | Refresh token TTL |
| `AUTH_VERSION` | 1 | Bump to revoke all sessions |
| `RATE_LIMIT_AUTH` | 5 | Max auth attempts per 10 min |
| `LOG_LEVEL` | DEBUG / INFO | Per-environment logging |
| `SENTRY_DSN` | — | Error tracking |
| `NTFY_TOPIC` | — | Operator notifications |
| `PI_SCREEN_CONTROL` | True | Raspberry Pi display scheduling |

---

## 16. Key Design Decisions

### Why SQLite?

- Single-server deployment — no need for client/server database
- WAL mode enables concurrent reads during writes
- Zero operational overhead (no separate process, no connection pooling, no backups daemon)
- 42 migrations proves the schema is actively evolving without issues
- Tradeoff: single-writer bottleneck (acceptable at household scale)

### Why no ORM?

- Raw SQL with helper functions in `db.py`
- Direct control over query optimization and migration behavior
- Lower abstraction overhead for a solo-developer project
- Tradeoff: more boilerplate per query, no automatic model validation

### Why two frontends?

- **Jinja2 pages** (`/kiosk`, `/mobile`) — production-active, server-rendered, stable
- **React SPA** (`src/`) — in-progress rewrite for a richer mobile experience
- The React frontend will eventually replace the Jinja2 pages once API wiring is complete

### Why self-healing agents?

- Household app with low tolerance for downtime (families rely on it daily)
- Agents detect and fix common issues before users notice
- All actions are audited and managers are notified — no silent changes

### Why local LLM (Ollama)?

- No API costs for grocery categorization (high-volume, low-stakes task)
- Privacy: household data stays on-premise
- Fallback to keyword heuristics when Ollama is unavailable
