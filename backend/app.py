"""
Huddle - Household Management System
FastAPI backend with SQLite database
"""

import logging
import sqlite3
import uuid
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pathlib import Path
from datetime import datetime
import asyncio
import uvicorn

# Core modules
from logging_config import setup_logging
from db import init_db, get_db
from settings import get_people, get_setting
from websocket import manager
from background import watchdog_and_maintenance_task, service_monitor_task, push_notification_task

# Routers
from routers import chores, calendar, meals, adhoc, bills, inventory, polls, settings_api, modules, shopping, status, recipes, pairing, routines, rewards, allowances, feedback, pets, noticeboard, assignments, expenses, house_rules, tenancy, freezer, selfcare, client_errors
from fuel import router as fuel_router
from push import router as push_router
from auth import router as auth_router, get_current_user, TenantContext

logger = logging.getLogger("huddle")

# Systemd watchdog support
try:
    import sdnotify
    SYSTEMD_NOTIFY = sdnotify.SystemdNotifier()
except ImportError:
    SYSTEMD_NOTIFY = None

app = FastAPI(title="Huddle", version="2.0.0")

# CORS for local network access
from config import CORS_ORIGINS, GA4_MEASUREMENT_ID
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "X-Requested-With", "Authorization"],
)

# GZip compression for responses over 1KB
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ---------------------------------------------------------------------------
# Request ID middleware — assigns a UUID to every request for log correlation
# ---------------------------------------------------------------------------
from logging_config import request_id_ctx


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Generate a unique request ID and attach it to the context for logging."""
    rid = uuid.uuid4().hex[:12]
    token = request_id_ctx.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    request_id_ctx.reset(token)
    return response


# Page view tracking — logic lives in page_views.py to avoid circular imports
# (background.py needs flush_page_views but cannot import from app.py).
from page_views import record_page_view, flush_page_views

# CSRF protection: paths exempt from X-Requested-With header check
_CSRF_EXEMPT_PATHS = {"/ws", "/api/billing/webhook"}
_CSRF_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


@app.middleware("http")
async def csrf_header_check(request: Request, call_next):
    """Require X-Requested-With: XMLHttpRequest on state-changing API requests.

    This mitigates CSRF attacks since custom headers cannot be set by
    cross-origin form submissions or simple navigations.
    Exempt: WebSocket upgrade, webhook endpoints, non-API paths, GET/HEAD/OPTIONS.
    """
    if (
        request.method in _CSRF_METHODS
        and request.url.path.startswith("/api/")
        and request.url.path not in _CSRF_EXEMPT_PATHS
    ):
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return JSONResponse(
                status_code=403,
                content={"detail": "Missing or invalid X-Requested-With header"},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Set security headers on all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=()"
    return response


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """Set Cache-Control headers for static assets."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Cache static files for 1 day, allow revalidation
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=3600"
    return response


@app.middleware("http")
async def track_page_views(request: Request, call_next):
    response = await call_next(request)
    if response.status_code < 400:
        record_page_view(request.url.path)
    return response


def _init_sentry():
    from config import SENTRY_DSN, ENVIRONMENT
    if not SENTRY_DSN:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENVIRONMENT,
            traces_sample_rate=0.1,
            send_default_pii=False,
        )
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
    except Exception as e:
        logger.warning("Sentry init failed: %s", e)

_init_sentry()

# Static files and templates
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["GA4_MEASUREMENT_ID"] = GA4_MEASUREMENT_ID

# ---------------------------------------------------------------------------
# Global exception handlers — safety net for any unhandled errors.
# Router-level try/except still takes priority; these catch anything that slips through.
# ---------------------------------------------------------------------------

@app.exception_handler(sqlite3.Error)
async def sqlite_exception_handler(request: Request, exc: sqlite3.Error):
    logger.error("Unhandled database error on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Database error"})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.critical("Unhandled error on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ============================================================================
# MOUNT ALL ROUTERS
# ============================================================================
app.include_router(chores.router)
app.include_router(calendar.router)
app.include_router(meals.router)
app.include_router(adhoc.router)
app.include_router(bills.router)
app.include_router(inventory.router)
app.include_router(polls.router)
app.include_router(settings_api.router)
app.include_router(modules.router)
app.include_router(shopping.router)
app.include_router(status.router)
app.include_router(recipes.router)
app.include_router(pairing.router)
app.include_router(routines.router)
app.include_router(rewards.router)
app.include_router(allowances.router)
app.include_router(feedback.router)
app.include_router(pets.router)
app.include_router(noticeboard.router)
app.include_router(assignments.router)
app.include_router(expenses.router)
app.include_router(house_rules.router)
app.include_router(tenancy.router)
app.include_router(freezer.router)
app.include_router(selfcare.router)
app.include_router(fuel_router)
app.include_router(push_router)
app.include_router(auth_router)
app.include_router(client_errors.router)


# ============================================================================
# HEALTH CHECK ENDPOINTS
# ============================================================================

@app.get("/api/health")
def health_check():
    """Full health check: database connectivity, timestamp, status."""
    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        tz = ZoneInfo(get_setting("timezone", "Australia/Sydney"))
        now = datetime.now(tz)

        checks = {}

        # Database check
        try:
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()
            checks["database"] = {"status": "ok"}
        except Exception as e:
            checks["database"] = {"status": "error", "error": str(e)}

        # Overall status
        all_ok = all(c["status"] == "ok" for c in checks.values())

        return {
            "status": "healthy" if all_ok else "degraded",
            "timestamp": now.isoformat(),
            "checks": checks,
        }
    except Exception as e:
        logger.error("Health check error: %s", e, exc_info=True)
        return {
            "status": "degraded",
            "timestamp": datetime.now().isoformat(),
            "checks": {"error": str(e)},
        }


@app.api_route("/api/health/ping", methods=["GET", "HEAD"])
async def health_ping():
    """Simple liveness probe."""
    return {"status": "ok"}


# ============================================================================
# PAGE ROUTES
# ============================================================================

def _validate_kiosk_token(token: str):
    """Validate a kiosk token and return household_id, or None if invalid."""
    if not token:
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT household_id FROM kiosk_tokens WHERE token = ?", (token,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE kiosk_tokens SET last_used_at = ? WHERE token = ?",
                    (datetime.now().isoformat(), token),
                )
                conn.commit()
                return row["household_id"]
        return None
    except Exception as e:
        logger.error("Failed to validate kiosk token: %s", e, exc_info=True)
        return None


def _get_any_kiosk_token():
    """Backward-compat: fetch the first kiosk token available (for local LAN Pi)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT token FROM kiosk_tokens ORDER BY last_used_at DESC LIMIT 1"
            ).fetchone()
            return row["token"] if row else ""
    except Exception as e:
        logger.error("Failed to fetch kiosk token: %s", e, exc_info=True)
        return ""


@app.get("/", response_class=HTMLResponse)
def kiosk_page(request: Request):
    """Main page. External requests via tunnel get redirected to the app. Local requests show kiosk."""
    try:
        host = request.headers.get("host", "")
        if "huddle.rodgersgroup.au" in host:
            return templates.TemplateResponse("landing.html", {"request": request})
        # Local network: show kiosk with first available token (backward-compat)
        kiosk_token = _get_any_kiosk_token()
        if not kiosk_token:
            return templates.TemplateResponse("kiosk_setup.html", {"request": request})
        response = templates.TemplateResponse("kiosk.html", {
            "request": request,
            "kiosk_token": kiosk_token,
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in kiosk_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/kiosk", response_class=HTMLResponse)
def kiosk_display_page(request: Request, token: str = None):
    """Kiosk display page. Requires a valid kiosk token via ?token= param."""
    try:
        kiosk_token = token or request.query_params.get("token", "")
        if kiosk_token:
            household_id = _validate_kiosk_token(kiosk_token)
            if household_id:
                response = templates.TemplateResponse("kiosk.html", {
                    "request": request,
                    "kiosk_token": kiosk_token,
                })
                response.headers["Cache-Control"] = "no-store"
                return response
        # No valid token — show setup screen
        error = "Invalid or expired kiosk token" if kiosk_token else None
        return templates.TemplateResponse("kiosk_setup.html", {
            "request": request,
            "error": error,
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in kiosk_display_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/mobile", response_class=HTMLResponse)
def mobile_page(request: Request):
    """Mobile PWA page. Redirects to /onboard if not authenticated."""
    from auth import COOKIE_NAME, verify_session_token
    token = request.cookies.get(COOKIE_NAME)
    if not token or not verify_session_token(token):
        return RedirectResponse(url="/onboard", status_code=302)
    try:
        return templates.TemplateResponse("mobile.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in mobile_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request):
    """Phone-side kiosk pairing page."""
    try:
        return templates.TemplateResponse("pair.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in pair_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/manifest.json")
def manifest():
    """PWA manifest."""
    return JSONResponse(content={
        "name": "Huddle",
        "short_name": "Huddle",
        "description": "Your household, organised",
        "start_url": "/mobile",
        "scope": "/",
        "display": "standalone",
        "background_color": "#1a1d27",
        "theme_color": "#4ecdc4",
        "orientation": "portrait",
        "categories": ["lifestyle", "utilities"],
        "icons": [
            {
                "src": "/static/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any"
            },
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable"
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            }
        ],
        "shortcuts": [
            {
                "name": "View Chores",
                "short_name": "Chores",
                "description": "See today's chores",
                "url": "/mobile?module=chores",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]
            },
            {
                "name": "Meal Plan",
                "short_name": "Meals",
                "description": "View this week's meals",
                "url": "/mobile?module=meals",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]
            },
            {
                "name": "Calendar",
                "short_name": "Calendar",
                "description": "Family calendar",
                "url": "/mobile?module=calendar",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]
            },
            {
                "name": "Shopping",
                "short_name": "Shopping",
                "description": "Shopping list",
                "url": "/mobile?module=shopping",
                "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]
            }
        ]
    })


@app.get("/sw.js")
def service_worker(v: str = None):
    """Service worker for PWA."""
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


@app.get("/.well-known/assetlinks.json")
def asset_links():
    """Digital Asset Links for Android TWA verification."""
    return FileResponse(
        BASE_DIR / "static" / ".well-known" / "assetlinks.json",
        media_type="application/json",
    )


@app.get("/onboard", response_class=HTMLResponse)
def onboard_page(request: Request):
    """Onboarding page: login, create household, or join household."""
    try:
        return templates.TemplateResponse("onboard.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in onboard_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/faq", response_class=HTMLResponse)
def faq_page(request: Request):
    return templates.TemplateResponse("docs/faq.html", {"request": request})


@app.get("/privacy-policy", response_class=HTMLResponse)
def privacy_policy_page(request: Request):
    return templates.TemplateResponse("docs/privacy-policy.html", {"request": request})


@app.get("/terms-of-service", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse("docs/terms-of-service.html", {"request": request})


@app.get("/beta-agreement", response_class=HTMLResponse)
def beta_agreement_page(request: Request):
    return templates.TemplateResponse("docs/beta-agreement.html", {"request": request})


@app.get("/pds", response_class=HTMLResponse)
def pds_page(request: Request):
    return templates.TemplateResponse("docs/pds.html", {"request": request})


@app.get("/getting-started", response_class=HTMLResponse)
def getting_started_page(request: Request):
    return templates.TemplateResponse("docs/getting-started.html", {"request": request})


# Super-admin check via config
from config import SUPERADMIN_EMAILS


async def require_superadmin(request: Request):
    """Check that the current user is a superadmin."""
    from auth import get_current_user, COOKIE_NAME, verify_session_token
    try:
        user = await get_current_user(request)
    except Exception:
        raise HTTPException(status_code=403, detail="Not authorised")
    with get_db() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user.user_id,)).fetchone()
    if not row or row["email"] not in SUPERADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Not authorised")
    return user


@app.get("/superadmin", response_class=HTMLResponse)
async def superadmin_page(request: Request):
    """Super-admin dashboard."""
    try:
        await require_superadmin(request)
        return templates.TemplateResponse("superadmin.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in superadmin_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/overview")
async def superadmin_overview(request: Request):
    """Return all households, users, and stats for superadmin."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            households = conn.execute(
                "SELECT h.*, COUNT(hm.id) as member_count FROM households h "
                "LEFT JOIN household_members hm ON hm.household_id = h.id "
                "GROUP BY h.id ORDER BY h.id"
            ).fetchall()
            users = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.created_at, "
                "hm.household_id, hm.role, h.name as household_name "
                "FROM users u "
                "LEFT JOIN household_members hm ON hm.user_id = u.id "
                "LEFT JOIN households h ON h.id = hm.household_id "
                "ORDER BY u.id"
            ).fetchall()
            # Single query for all global counts (was 15 separate queries)
            counts_row = conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM users) as total_users,
                    (SELECT COUNT(*) FROM households) as total_households,
                    (SELECT COUNT(*) FROM chores) as total_chores,
                    (SELECT COUNT(*) FROM completions) as total_completions,
                    (SELECT COUNT(*) FROM meals) as total_meals,
                    (SELECT COUNT(*) FROM bills) as total_bills,
                    (SELECT COUNT(*) FROM shopping_items) as total_shopping_items,
                    (SELECT COUNT(*) FROM recipes) as total_recipes,
                    (SELECT COUNT(*) FROM calendar_events) as total_calendar_events,
                    (SELECT COUNT(*) FROM adhoc_tasks) as total_adhoc_tasks,
                    (SELECT COUNT(*) FROM polls) as total_polls,
                    (SELECT COUNT(*) FROM users WHERE last_login_at > datetime('now', '-7 days')) as active_users_7d,
                    (SELECT COUNT(*) FROM users WHERE last_login_at > datetime('now', '-30 days')) as active_users_30d,
                    (SELECT COUNT(*) FROM completions WHERE completed_at > datetime('now', '-7 days')) as completions_7d,
                    (SELECT COUNT(*) FROM push_subscriptions) as push_subscriptions
            """).fetchone()
            stats = dict(counts_row)
            # Segment and tier aggregation
            try:
                seg_rows = conn.execute(
                    "SELECT COALESCE(segment, 'household') as seg, COUNT(*) as c FROM households GROUP BY seg"
                ).fetchall()
                stats["households_by_segment"] = {r["seg"]: r["c"] for r in seg_rows}
                tier_rows = conn.execute(
                    "SELECT COALESCE(tier, 'free') as t, COUNT(*) as c FROM households GROUP BY t"
                ).fetchall()
                stats["households_by_tier"] = {r["t"]: r["c"] for r in tier_rows}
            except Exception as e:
                logger.warning("Superadmin segment/tier aggregation error: %s", e)
                stats["households_by_segment"] = {}
                stats["households_by_tier"] = {}
            # Page view stats (last 7 and 30 days)
            try:
                pv_7d = conn.execute(
                    "SELECT page, SUM(count) as total FROM page_views WHERE view_date > date('now', '-7 days') GROUP BY page ORDER BY total DESC"
                ).fetchall()
                pv_30d = conn.execute(
                    "SELECT page, SUM(count) as total FROM page_views WHERE view_date > date('now', '-30 days') GROUP BY page ORDER BY total DESC"
                ).fetchall()
                stats["page_views_7d"] = {r["page"]: r["total"] for r in pv_7d}
                stats["page_views_30d"] = {r["page"]: r["total"] for r in pv_30d}
            except Exception as e:
                logger.warning("Superadmin page view stats error: %s", e)
                stats["page_views_7d"] = {}
                stats["page_views_30d"] = {}

            # Feedback aggregation across all households
            feedback_data = {}
            try:
                # Counts by type
                type_counts = conn.execute(
                    "SELECT feedback_type, COUNT(*) as c FROM feedback GROUP BY feedback_type"
                ).fetchall()
                feedback_data["by_type"] = {r["feedback_type"]: r["c"] for r in type_counts}

                # Counts by status
                status_counts = conn.execute(
                    "SELECT status, COUNT(*) as c FROM feedback GROUP BY status"
                ).fetchall()
                feedback_data["by_status"] = {r["status"]: r["c"] for r in status_counts}

                # Most recent 20
                recent = conn.execute("""
                    SELECT f.*, h.name as household_name, COUNT(v.id) as vote_count
                    FROM feedback f
                    LEFT JOIN households h ON h.id = f.household_id
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    GROUP BY f.id
                    ORDER BY f.created_at DESC LIMIT 20
                """).fetchall()
                feedback_data["recent"] = [dict(r) for r in recent]

                # Most upvoted (top 10)
                top_voted = conn.execute("""
                    SELECT f.*, h.name as household_name, COUNT(v.id) as vote_count
                    FROM feedback f
                    LEFT JOIN households h ON h.id = f.household_id
                    LEFT JOIN feedback_votes v ON v.feedback_id = f.id
                    GROUP BY f.id
                    HAVING vote_count > 0
                    ORDER BY vote_count DESC LIMIT 10
                """).fetchall()
                feedback_data["top_voted"] = [dict(r) for r in top_voted]

                feedback_data["total"] = conn.execute("SELECT COUNT(*) as c FROM feedback").fetchone()["c"]
            except Exception as e:
                logger.warning("Superadmin feedback aggregation error: %s", e)
                feedback_data = {"error": str(e)}

        return {
            "households": [dict(h) for h in households],
            "users": [dict(u) for u in users],
            "stats": stats,
            "feedback": feedback_data,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_overview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_overview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/superadmin/household/{household_id}")
async def superadmin_household_detail(household_id: int, request: Request):
    """Get complete household details for superadmin troubleshooting."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            household = conn.execute(
                "SELECT * FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found")

            members = conn.execute(
                """SELECT hm.id, hm.user_id, hm.display_name, hm.color, hm.role, hm.joined_at,
                          u.email
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ?
                   ORDER BY hm.joined_at""",
                (household_id,),
            ).fetchall()

            settings_rows = conn.execute(
                "SELECT key, value FROM settings WHERE household_id = ?",
                (household_id,),
            ).fetchall()

            module_rows = conn.execute(
                "SELECT module_key, enabled FROM household_modules WHERE household_id = ?",
                (household_id,),
            ).fetchall()
            enabled_map = {r["module_key"]: r["enabled"] == 1 for r in module_rows}

            stats = {
                "chores": conn.execute(
                    "SELECT COUNT(*) as c FROM chores WHERE household_id = ?", (household_id,)
                ).fetchone()["c"],
                "completions": conn.execute(
                    "SELECT COUNT(*) as c FROM completions WHERE household_id = ?", (household_id,)
                ).fetchone()["c"],
                "bills": conn.execute(
                    "SELECT COUNT(*) as c FROM bills WHERE household_id = ?", (household_id,)
                ).fetchone()["c"],
                "tasks": conn.execute(
                    "SELECT COUNT(*) as c FROM adhoc_tasks WHERE household_id = ?", (household_id,)
                ).fetchone()["c"],
            }

        from routers.modules import AVAILABLE_MODULES
        modules = []
        for key, meta in AVAILABLE_MODULES.items():
            modules.append({
                "key": key,
                "name": meta["name"],
                "icon": meta.get("icon", ""),
                "enabled": enabled_map.get(key, True),
            })

        return {
            "household": dict(household),
            "members": [dict(m) for m in members],
            "settings": {r["key"]: r["value"] for r in settings_rows},
            "modules": modules,
            "stats": stats,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_detail: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_detail: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put("/api/superadmin/household/{household_id}/member/{member_id}/role")
async def superadmin_change_member_role(household_id: int, member_id: int, request: Request):
    """Change a household member's role (superadmin)."""
    try:
        await require_superadmin(request)
        data = await request.json()
        role = data.get("role")
        if role not in ("manager", "member", "dependent"):
            raise HTTPException(status_code=400, detail="Role must be manager, member, or dependent")

        with get_db() as conn:
            member = conn.execute(
                "SELECT id, role FROM household_members WHERE id = ? AND household_id = ?",
                (member_id, household_id),
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found in household")

            if role == "manager" and member["role"] != "manager":
                manager_count = conn.execute(
                    "SELECT COUNT(*) as c FROM household_members WHERE household_id = ? AND role = 'manager'",
                    (household_id,),
                ).fetchone()["c"]
                if manager_count >= 5:
                    raise HTTPException(status_code=400, detail="Maximum 5 managers per household")

            conn.execute(
                "UPDATE household_members SET role = ? WHERE id = ? AND household_id = ?",
                (role, member_id, household_id),
            )
            conn.commit()
        return {"ok": True, "member_id": member_id, "role": role}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_change_member_role: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_change_member_role: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/superadmin/household/{household_id}/member/{member_id}")
async def superadmin_remove_member(household_id: int, member_id: int, request: Request):
    """Remove a member from a household (superadmin)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            member = conn.execute(
                "SELECT id, display_name FROM household_members WHERE id = ? AND household_id = ?",
                (member_id, household_id),
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found in household")
            conn.execute(
                "DELETE FROM household_members WHERE id = ? AND household_id = ?",
                (member_id, household_id),
            )
            conn.commit()
        return {"ok": True, "removed": member["display_name"]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_remove_member: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_remove_member: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/superadmin/household/{household_id}/member")
async def superadmin_add_member(household_id: int, request: Request):
    """Add a placeholder member to a household (superadmin)."""
    try:
        await require_superadmin(request)
        data = await request.json()
        name = (data.get("name") or "").strip()
        role = data.get("role", "member")
        color = data.get("color", "#4ecdc4")

        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        if role not in ("manager", "member", "dependent"):
            raise HTTPException(status_code=400, detail="Invalid role")

        with get_db() as conn:
            # Check household exists
            hh = conn.execute("SELECT id FROM households WHERE id = ?", (household_id,)).fetchone()
            if not hh:
                raise HTTPException(status_code=404, detail="Household not found")

            # Check for duplicate name
            existing = conn.execute(
                "SELECT id FROM household_members WHERE household_id = ? AND display_name = ?",
                (household_id, name)
            ).fetchone()
            if existing:
                raise HTTPException(status_code=400, detail="Name already exists in household")

            # Create placeholder user
            email = f"{name.lower().replace(' ', '_')}@placeholder"
            existing_user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing_user:
                user_id = existing_user["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO users (email, display_name) VALUES (?, ?)",
                    (email, name)
                )
                user_id = cursor.lastrowid

            conn.execute(
                "INSERT INTO household_members (household_id, user_id, display_name, color, role) VALUES (?, ?, ?, ?, ?)",
                (household_id, user_id, name, color, role)
            )
            conn.commit()

        return {"ok": True, "name": name}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_add_member: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_add_member: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put("/api/superadmin/household/{household_id}/settings")
async def superadmin_update_settings(household_id: int, request: Request):
    """Update settings for any household (superadmin)."""
    try:
        await require_superadmin(request)
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        with get_db() as conn:
            if not conn.execute("SELECT id FROM households WHERE id = ?", (household_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Household not found")

        from settings import save_setting
        for key, value in data.items():
            save_setting(key, value, household_id=household_id)
        return {"message": f"Updated {len(data)} settings"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_update_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_update_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put("/api/superadmin/household/{household_id}/modules/{module_key}")
async def superadmin_toggle_module(household_id: int, module_key: str, request: Request):
    """Toggle a module for any household (superadmin)."""
    try:
        await require_superadmin(request)
        from routers.modules import AVAILABLE_MODULES
        if module_key not in AVAILABLE_MODULES:
            raise HTTPException(status_code=404, detail=f"Unknown module: {module_key}")

        data = await request.json()
        if "enabled" not in data or not isinstance(data["enabled"], bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        enabled = 1 if data["enabled"] else 0

        with get_db() as conn:
            if not conn.execute("SELECT id FROM households WHERE id = ?", (household_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Household not found")
            conn.execute("""
                INSERT INTO household_modules (household_id, module_key, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(household_id, module_key) DO UPDATE SET enabled = ?
            """, (household_id, module_key, enabled, enabled))
            conn.commit()
        return {"module": module_key, "enabled": enabled == 1}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_toggle_module: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_toggle_module: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/beta-modules")
async def superadmin_list_beta_modules(request: Request):
    """List all modules with their beta status."""
    try:
        await require_superadmin(request)
        from routers.modules import AVAILABLE_MODULES, _get_beta_module_keys
        beta_keys = _get_beta_module_keys()
        result = []
        for key, meta in AVAILABLE_MODULES.items():
            result.append({
                "key": key,
                "name": meta["name"],
                "beta": key in beta_keys,
            })
        return {"modules": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in superadmin_list_beta_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put("/api/superadmin/beta-modules/{module_key}")
async def superadmin_toggle_beta(module_key: str, request: Request):
    """Toggle a module's beta status. beta=true means only superadmin sees it."""
    try:
        await require_superadmin(request)
        from routers.modules import AVAILABLE_MODULES, _clear_beta_cache
        if module_key not in AVAILABLE_MODULES:
            raise HTTPException(status_code=404, detail=f"Unknown module: {module_key}")

        data = await request.json()
        if "beta" not in data or not isinstance(data["beta"], bool):
            raise HTTPException(status_code=400, detail="beta must be a boolean")

        with get_db() as conn:
            if data["beta"]:
                conn.execute(
                    "INSERT OR IGNORE INTO module_beta (module_key) VALUES (?)",
                    (module_key,),
                )
            else:
                conn.execute(
                    "DELETE FROM module_beta WHERE module_key = ?",
                    (module_key,),
                )
            conn.commit()

        _clear_beta_cache()
        logger.info("Module %s beta status set to %s", module_key, data["beta"])
        return {"module": module_key, "beta": data["beta"]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_toggle_beta: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_toggle_beta: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/superadmin/impersonate/{household_id}")
async def superadmin_impersonate(household_id: int, request: Request, response: Response):
    """Impersonate a household by switching the session to target household_id."""
    try:
        user = await require_superadmin(request)
        with get_db() as conn:
            household = conn.execute(
                "SELECT id FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found")

        from auth import create_session_token, COOKIE_NAME, COOKIE_SECURE
        # Preserve original household so we can restore later
        original = user.original_household_id or user.household_id
        token = create_session_token(
            user_id=user.user_id,
            household_id=household_id,
            original_household_id=original,
        )
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=2592000,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        logger.info("Superadmin %s impersonating household %s", user.user_id, household_id)
        return {"ok": True, "household_id": household_id}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_impersonate: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_impersonate: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/superadmin/unimpersonate")
async def superadmin_unimpersonate(request: Request, response: Response):
    """Stop impersonating and restore the original session."""
    try:
        from auth import get_current_user, create_session_token, COOKIE_NAME, COOKIE_SECURE
        user = await get_current_user(request)
        if not user.original_household_id:
            raise HTTPException(status_code=400, detail="Not currently impersonating")

        token = create_session_token(
            user_id=user.user_id,
            household_id=user.original_household_id,
        )
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=2592000,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        logger.info("Superadmin %s stopped impersonating, restored household %s", user.user_id, user.original_household_id)
        return {"ok": True, "household_id": user.original_household_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in superadmin_unimpersonate: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/chores")
async def superadmin_household_chores(household_id: int, request: Request):
    """List all chores for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                """SELECT c.*,
                   (SELECT COUNT(*) FROM completions WHERE chore_id = c.id) as completion_count
                   FROM chores c WHERE c.household_id = ? ORDER BY c.name""",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_chores: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_chores: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/tasks")
async def superadmin_household_tasks(household_id: int, request: Request):
    """List all ad-hoc tasks for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM adhoc_tasks WHERE household_id = ? ORDER BY created_at DESC",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_tasks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_tasks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/calendar")
async def superadmin_household_calendar(household_id: int, request: Request):
    """List all calendar events for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE household_id = ? ORDER BY start_date DESC",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_calendar: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_calendar: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/bills")
async def superadmin_household_bills(household_id: int, request: Request):
    """List all bills for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM bills WHERE household_id = ? ORDER BY due_date DESC",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_bills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_bills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/shopping")
async def superadmin_household_shopping(household_id: int, request: Request):
    """List all shopping items for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM shopping_items WHERE household_id = ? ORDER BY created_at DESC",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/inventory")
async def superadmin_household_inventory(household_id: int, request: Request):
    """List all inventory items for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM inventory_items WHERE household_id = ? ORDER BY name",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/polls")
async def superadmin_household_polls(household_id: int, request: Request):
    """List all polls for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                """SELECT p.*, COUNT(pv.id) as vote_count
                   FROM polls p
                   LEFT JOIN poll_votes pv ON pv.poll_id = p.id
                   WHERE p.household_id = ?
                   GROUP BY p.id
                   ORDER BY p.created_at DESC""",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_polls: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_polls: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/superadmin/household/{household_id}/recipes")
async def superadmin_household_recipes(household_id: int, request: Request):
    """List all recipes for a household (superadmin data browser)."""
    try:
        await require_superadmin(request)
        with get_db() as conn:
            rows = conn.execute(
                """SELECT r.*, COUNT(ri.id) as ingredient_count
                   FROM recipes r
                   LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                   WHERE r.household_id = ?
                   GROUP BY r.id
                   ORDER BY r.name""",
                (household_id,),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_household_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_household_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/superadmin/household/{household_id}/{module}/{item_id}")
async def superadmin_delete_item(household_id: int, module: str, item_id: int, request: Request):
    """Delete an item from any module in any household (superadmin)."""
    try:
        await require_superadmin(request)

        table_map = {
            "chores": "chores",
            "tasks": "adhoc_tasks",
            "calendar": "calendar_events",
            "bills": "bills",
            "shopping": "shopping_items",
            "inventory": "inventory_items",
            "polls": "polls",
            "recipes": "recipes",
        }
        table = table_map.get(module)
        if not table:
            raise HTTPException(status_code=400, detail=f"Unknown module: {module}")

        with get_db() as conn:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Item not found")
            conn.execute(
                f"DELETE FROM {table} WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            )
            conn.commit()
        logger.info("Superadmin deleted %s/%s from household %s", module, item_id, household_id)
        return {"ok": True, "module": module, "item_id": item_id}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in superadmin_delete_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in superadmin_delete_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/superadmin/sandbox")
async def create_sandbox_household(request: Request):
    """Create a sandbox household with demo data (superadmin)."""
    try:
        tenant = await require_superadmin(request)
        body = await request.json()
        preset = body.get("preset", "family")

        from scripts.sandbox_generator import create_sandbox
        with get_db() as conn:
            result = create_sandbox(conn, preset, tenant.user_id)

        logger.info("Superadmin created sandbox household: %s (preset=%s)", result["name"], preset)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error creating sandbox: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error creating sandbox: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/superadmin/sandbox/{household_id}")
async def delete_sandbox_household(household_id: int, request: Request):
    """Delete a sandbox household and all its data (superadmin)."""
    try:
        await require_superadmin(request)

        from scripts.sandbox_generator import delete_sandbox
        with get_db() as conn:
            delete_sandbox(conn, household_id)

        logger.info("Superadmin deleted sandbox household %s", household_id)
        return {"ok": True, "message": "Sandbox deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error deleting sandbox: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error deleting sandbox: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/household", response_class=HTMLResponse)
def household_page(request: Request):
    """Household settings: members, invites, modules, kiosk tokens."""
    try:
        return templates.TemplateResponse("household.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in household_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    """Status and changelog page."""
    try:
        return templates.TemplateResponse("status.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in status_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/legal", response_class=HTMLResponse)
def legal_page(request: Request):
    """Legal information, privacy policy, terms, FAQ, and getting started."""
    section = request.query_params.get("section", "")
    return templates.TemplateResponse("legal.html", {"request": request, "section": section})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Admin page for managing chores."""
    try:
        return templates.TemplateResponse("admin.html", {"request": request, "people": get_people(household_id=tenant.household_id)})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in admin_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    """Family calendar month view page."""
    try:
        return templates.TemplateResponse("calendar.html", {"request": request})
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in calendar_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/complete/{chore_id}", response_class=HTMLResponse)
def complete_page(request: Request, chore_id: int):
    """Mobile completion page."""
    try:
        with get_db() as conn:
            chore = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
            if not chore:
                raise HTTPException(status_code=404, detail="Chore not found")
            return templates.TemplateResponse("complete.html", {
                "request": request,
                "chore": dict(chore),
                "people": get_people(household_id=chore["household_id"])
            })
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/complete/{chore_id}/{slug}", response_class=HTMLResponse)
def complete_page_with_slug(request: Request, chore_id: int, slug: str):
    """Mobile completion page with readable slug (slug is ignored, just for readability)."""
    try:
        with get_db() as conn:
            chore = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
            if not chore:
                raise HTTPException(status_code=404, detail="Chore not found")
            return templates.TemplateResponse("complete.html", {
                "request": request,
                "chore": dict(chore),
                "people": get_people(household_id=chore["household_id"])
            })
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_page_with_slug: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_page_with_slug: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/adhoc/{task_id}", response_class=HTMLResponse)
def adhoc_page(request: Request, task_id: int):
    """Ad-hoc task completion page."""
    try:
        with get_db() as conn:
            task = conn.execute("SELECT * FROM adhoc_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            return templates.TemplateResponse("adhoc.html", {
                "request": request,
                "task": dict(task),
                "people": get_people(household_id=task["household_id"])
            })
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in adhoc_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in adhoc_page: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/adhoc/{task_id}/{slug}", response_class=HTMLResponse)
def adhoc_page_with_slug(request: Request, task_id: int, slug: str):
    """Ad-hoc task completion page with readable slug."""
    try:
        with get_db() as conn:
            task = conn.execute("SELECT * FROM adhoc_tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            return templates.TemplateResponse("adhoc.html", {
                "request": request,
                "task": dict(task),
                "people": get_people(household_id=task["household_id"])
            })
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in adhoc_page_with_slug: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in adhoc_page_with_slug: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# WEBSOCKET
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates, scoped to household."""
    household_id = None

    # Try session cookie first (mobile/calendar users)
    from auth import COOKIE_NAME, verify_session_token
    session_cookie = websocket.cookies.get(COOKIE_NAME)
    if session_cookie:
        data = verify_session_token(session_cookie)
        if data and data.get("household_id", 0) > 0:
            household_id = data["household_id"]

    # Try kiosk token query param (kiosk displays)
    if household_id is None:
        kiosk_token = websocket.query_params.get("token")
        if kiosk_token:
            try:
                with get_db() as conn:
                    kt = conn.execute(
                        "SELECT household_id FROM kiosk_tokens WHERE token = ?",
                        (kiosk_token,),
                    ).fetchone()
                    if kt:
                        household_id = kt["household_id"]
            except Exception as e:
                logger.debug("WebSocket kiosk token lookup failed: %s", e)

    if household_id is None:
        await websocket.accept()
        await websocket.close(code=4001, reason="Authentication required")
        return

    await manager.connect(websocket, household_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket error: %s", e)
    finally:
        manager.disconnect(websocket, household_id)


# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
async def startup():
    setup_logging()
    from config import check_config, log_config
    check_config()
    log_config()
    init_db()

    # Notify systemd that we're ready
    if SYSTEMD_NOTIFY:
        SYSTEMD_NOTIFY.notify("READY=1")
        logger.info("Systemd notified: READY")

    # Start background maintenance task
    asyncio.create_task(watchdog_and_maintenance_task())
    logger.info("Background maintenance task started")

    # Start service monitor for screen keepalive and fuel freshness
    asyncio.create_task(service_monitor_task())
    logger.info("Service monitor task started")

    # Start push notification task
    asyncio.create_task(push_notification_task())
    logger.info("Push notification task started")

    # Start self-healing agents loop
    from agents.loop import agent_loop_task
    asyncio.create_task(agent_loop_task())
    logger.info("Self-healing agents loop started")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
