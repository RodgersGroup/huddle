"""
Authentication module for Huddle.

Supports two auth methods:
1. Email + password (AnyList-style) → access/refresh token pair
2. Magic link (passwordless) → session cookie (legacy, still supported)

Token auth flow:
  POST /api/auth/signup   → create account with email+password → tokens
  POST /api/auth/token    → login with email+password → tokens
  POST /api/auth/token/refresh → refresh access token with refresh token
  POST /api/auth/logout   → revoke session

Also provides household creation/joining, kiosk token management, and FastAPI
dependencies for extracting tenant context from requests.
"""

import hashlib
import logging
import sqlite3
import time
import uuid
from fastapi import APIRouter, HTTPException, Request, Response, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
import json
import secrets
import string
import os
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from db import get_db
from settings import get_setting

# Password hashing
try:
    import bcrypt
    BCRYPT_ENABLED = True
except ImportError:
    BCRYPT_ENABLED = False

logger = logging.getLogger("huddle")


class RateLimiter:
    """Simple in-memory rate limiter tracking requests by IP address.

    LIMITATION: This rate limiter stores state in process memory, so it resets
    on every service restart and does not share state across multiple workers.
    For a single-worker Uvicorn deployment (our current setup) this is fine,
    but if Huddle ever scales to multiple workers or processes, this should be
    replaced with a Redis-backed or SQLite-backed rate limiter to maintain
    accurate counts across restarts and workers.
    """

    def __init__(self):
        self._requests = {}  # ip -> list of timestamps
        self._last_cleanup = time.time()

    def _cleanup(self):
        """Remove entries older than 15 minutes every 5 minutes."""
        now = time.time()
        if now - self._last_cleanup < 300:
            return
        cutoff = now - 900
        self._requests = {
            ip: [t for t in timestamps if t > cutoff]
            for ip, timestamps in self._requests.items()
            if any(t > cutoff for t in timestamps)
        }
        self._last_cleanup = now

    def is_limited(self, ip: str, max_requests: int, window_seconds: int) -> bool:
        """Check if IP has exceeded the rate limit. Returns True if blocked."""
        self._cleanup()
        now = time.time()
        cutoff = now - window_seconds
        timestamps = self._requests.get(ip, [])
        recent = [t for t in timestamps if t > cutoff]
        recent.append(now)
        self._requests[ip] = recent
        if len(recent) > max_requests:
            logger.warning("Rate limit hit: %s (%d/%d in %ds)", ip, len(recent), max_requests, window_seconds)
            return True
        return False


rate_limiter = RateLimiter()

AVATAR_DIR = Path(__file__).parent / "static" / "avatars"
AVATAR_MAX_SIZE = 10 * 1024 * 1024  # 10MB

try:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    AUTH_ENABLED = True
except ImportError:
    AUTH_ENABLED = False
    logger.warning("itsdangerous not installed - auth disabled")

try:
    import resend
    EMAIL_ENABLED = True
except ImportError:
    EMAIL_ENABLED = False
    logger.warning("resend not installed - magic link emails disabled (will print to console)")


# Configuration
from config import SECRET_KEY, SESSION_MAX_AGE, RATE_LIMIT_AUTH, ENVIRONMENT, ACCESS_TOKEN_MAX_AGE, REFRESH_TOKEN_MAX_AGE, AUTH_VERSION

# ---------------------------------------------------------------------------
# Auth version — cached read from file, fallback to config constant
# ---------------------------------------------------------------------------
_AUTH_VERSION_FILE = Path(__file__).parent / "auth_version.txt"
_auth_version_cache = None
_auth_version_ts = 0.0

def get_auth_version() -> int:
    """Return the current auth version (file takes priority over env/config).

    Cached for 60 seconds to avoid disk reads on every request.
    """
    global _auth_version_cache, _auth_version_ts
    now = time.time()
    if _auth_version_cache is not None and (now - _auth_version_ts) < 60:
        return _auth_version_cache
    try:
        v = int(_AUTH_VERSION_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        v = AUTH_VERSION
    _auth_version_cache = v
    _auth_version_ts = now
    return v


def force_reauth_all() -> int:
    """Bump auth version and revoke all DB sessions. Returns count of revoked sessions."""
    global _auth_version_cache, _auth_version_ts
    new_version = get_auth_version() + 1
    _AUTH_VERSION_FILE.write_text(str(new_version))
    # Clear cache so next call picks up the new version immediately
    _auth_version_cache = None
    _auth_version_ts = 0.0
    with get_db() as conn:
        cursor = conn.execute("UPDATE sessions SET revoked = 1 WHERE revoked = 0")
        conn.commit()
        return cursor.rowcount
SERIALIZER = URLSafeTimedSerializer(SECRET_KEY) if AUTH_ENABLED else None
MAGIC_LINK_MAX_AGE = 1800  # 30 minutes
COOKIE_NAME = "huddle_session"
COOKIE_SECURE = ENVIRONMENT == "production"
MIN_PASSWORD_LENGTH = 8
_SESSION_TOUCH_INTERVAL = 300  # Only update last_used_at every 5 minutes


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a password with bcrypt. Returns the hash string."""
    if not BCRYPT_ENABLED:
        raise RuntimeError("bcrypt is not installed")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash."""
    if not BCRYPT_ENABLED:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# ---------------------------------------------------------------------------
# Access / Refresh token management (AnyList-style)
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, household_id: int, session_id: str) -> str:
    """Create a short-lived signed access token (15 min default)."""
    if not SERIALIZER:
        raise RuntimeError("Auth is not enabled (itsdangerous not installed)")
    return SERIALIZER.dumps({
        "user_id": user_id,
        "household_id": household_id,
        "session_id": session_id,
        "type": "access",
        "v": get_auth_version(),
    })


def verify_access_token(token: str):
    """Verify an access token. Returns payload dict or None."""
    if not SERIALIZER:
        return None
    try:
        data = SERIALIZER.loads(token, max_age=ACCESS_TOKEN_MAX_AGE)
        if data.get("type") == "access" and "user_id" in data:
            if data.get("v", 0) < get_auth_version():
                return None  # Token predates auth version bump
            return data
        return None
    except (BadSignature, SignatureExpired):
        return None


def create_session_with_tokens(
    user_id: int,
    household_id: int,
    device_id: str = None,
    device_name: str = None,
    ip_address: str = None,
) -> dict:
    """Create a new session and return access_token + refresh_token + session info.

    This is the AnyList-style auth response: the client stores both tokens,
    uses access_token for API calls, and calls /auth/token/refresh when it expires.
    """
    session_id = uuid.uuid4().hex
    refresh_token = secrets.token_urlsafe(64)
    now = datetime.now()
    expires_at = (now + timedelta(seconds=REFRESH_TOKEN_MAX_AGE)).isoformat()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO sessions (id, user_id, household_id, refresh_token,
                   device_id, device_name, ip_address, created_at, last_used_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, household_id, refresh_token,
             device_id, device_name, ip_address, now.isoformat(), now.isoformat(), expires_at),
        )
        conn.commit()

    access_token = create_access_token(user_id, household_id, session_id)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_MAX_AGE,
        "user_id": user_id,
        "household_id": household_id,
        "session_id": session_id,
    }


def refresh_session_tokens(refresh_token: str, ip_address: str = None) -> dict:
    """Validate a refresh token and issue a new token pair.

    Implements token rotation: the old refresh token is revoked and a new one
    is issued. This means a stolen refresh token can only be used once.
    """
    now = datetime.now()

    with get_db() as conn:
        session = conn.execute(
            """SELECT id, user_id, household_id, device_id, device_name, expires_at
               FROM sessions
               WHERE refresh_token = ? AND revoked = 0""",
            (refresh_token,),
        ).fetchone()

        if not session:
            raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")

        if session["expires_at"] < now.isoformat():
            # Expired — revoke and reject
            conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session["id"],))
            conn.commit()
            raise HTTPException(status_code=401, detail="Refresh token expired")

        # Token rotation: revoke old, create new
        conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session["id"],))
        conn.commit()

    # Create a fresh session (new session ID + new refresh token)
    return create_session_with_tokens(
        user_id=session["user_id"],
        household_id=session["household_id"],
        device_id=session["device_id"],
        device_name=session["device_name"],
        ip_address=ip_address,
    )


def revoke_session(session_id: str):
    """Revoke a specific session (logout from one device)."""
    with get_db() as conn:
        conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,))
        conn.commit()


def revoke_all_sessions(user_id: int, except_session_id: str = None):
    """Revoke all sessions for a user, optionally keeping one (current session)."""
    with get_db() as conn:
        if except_session_id:
            conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND id != ? AND revoked = 0",
                (user_id, except_session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
                (user_id,),
            )
        conn.commit()


def _touch_session(conn, session_id: str):
    """Update last_used_at on a session, throttled to avoid a write per request."""
    row = conn.execute(
        "SELECT last_used_at FROM sessions WHERE id = ? AND revoked = 0",
        (session_id,),
    ).fetchone()
    if row:
        try:
            last = datetime.fromisoformat(row["last_used_at"])
            if (datetime.now() - last).total_seconds() < _SESSION_TOUCH_INTERVAL:
                return
        except (ValueError, TypeError):
            pass
        conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?",
            (datetime.now().isoformat(), session_id),
        )
        conn.commit()


def _parse_user_agent(ua: str) -> str:
    """Extract a human-readable device name from a User-Agent string."""
    if not ua:
        return "Unknown device"
    ua_lower = ua.lower()
    # Mobile
    if "iphone" in ua_lower:
        return "iPhone"
    if "ipad" in ua_lower:
        return "iPad"
    if "android" in ua_lower:
        return "Android"
    # Desktop browsers
    if "firefox" in ua_lower:
        return "Firefox"
    if "edg" in ua_lower:
        return "Edge"
    if "chrome" in ua_lower:
        return "Chrome"
    if "safari" in ua_lower:
        return "Safari"
    return ua[:50]


@dataclass
class TenantContext:
    user_id: int
    household_id: int
    display_name: str
    role: str
    original_household_id: int = None  # Set when superadmin is impersonating


def require_role(*allowed_roles):
    """Return a FastAPI dependency that checks the current user has one of the allowed roles.

    Usage::

        @router.post("/api/things")
        async def create_thing(tenant: TenantContext = Depends(require_role("manager", "member"))):
            ...
    """
    async def checker(tenant: TenantContext = Depends(get_current_user)):
        if tenant.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return tenant
    return checker


def create_session_token(user_id: int, household_id: int, original_household_id: int = None) -> str:
    """Create a signed session token encoding user and household IDs."""
    if not SERIALIZER:
        raise RuntimeError("Auth is not enabled (itsdangerous not installed)")
    payload = {"user_id": user_id, "household_id": household_id, "v": get_auth_version()}
    if original_household_id is not None:
        payload["original_household_id"] = original_household_id
    return SERIALIZER.dumps(payload)


def verify_session_token(token: str):
    """Validate a session token and return {user_id, household_id} or None."""
    if not SERIALIZER:
        return None
    try:
        data = SERIALIZER.loads(token, max_age=SESSION_MAX_AGE)
        if "user_id" in data and "household_id" in data:
            if data.get("v", 0) < get_auth_version():
                return None  # Cookie predates auth version bump
            return data
        return None
    except (BadSignature, SignatureExpired):
        return None


def _build_magic_link_email(verify_url: str) -> str:
    """Build a branded HTML email for the magic link login."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 20px;">
<tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:440px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
  <tr><td style="background:#1a1d27;padding:32px 24px;text-align:center;">
    <div style="font-size:32px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">
      <span style="color:#4ecdc4;">H</span>uddle
    </div>
    <div style="color:#888;font-size:14px;margin-top:4px;">Your household, sorted.</div>
  </td></tr>
  <tr><td style="padding:32px 28px;">
    <h1 style="margin:0 0 8px;font-size:20px;font-weight:700;color:#1a1d27;">Log in to Huddle</h1>
    <p style="margin:0 0 24px;color:#555;font-size:15px;line-height:1.5;">
      Tap the button below to log in. No password needed!
    </p>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center">
        <a href="{verify_url}"
           style="display:inline-block;background:#4ecdc4;color:#1a1d27;padding:14px 40px;border-radius:12px;font-size:16px;font-weight:700;text-decoration:none;letter-spacing:0.3px;">
          Log in to Huddle
        </a>
      </td></tr>
    </table>
    <p style="margin:24px 0 0;color:#999;font-size:13px;line-height:1.5;">
      This link expires in <strong>30 minutes</strong>. If you didn't request this, you can safely ignore it.
    </p>
  </td></tr>
  <tr><td style="border-top:1px solid #eee;padding:20px 28px;text-align:center;">
    <p style="margin:0;color:#bbb;font-size:12px;">
      Can't click the button? Copy this link:<br>
      <a href="{verify_url}" style="color:#4ecdc4;word-break:break-all;font-size:11px;">{verify_url}</a>
    </p>
  </td></tr>
</table>
<p style="margin:20px 0 0;color:#bbb;font-size:11px;text-align:center;">
  Huddle &mdash; huddle.rodgersgroup.au
</p>
</td></tr>
</table>
</body>
</html>"""


def _send_member_invite_email(email: str, name: str, household_name: str, base_url: str):
    """Send a branded invite email to a new household member with a magic login link."""
    try:
        import resend
        from config import RESEND_API_KEY

        if not RESEND_API_KEY:
            logger.info("INVITE for %s (%s): Would send invite email but no RESEND_API_KEY", name, email)
            return

        resend.api_key = RESEND_API_KEY

        # Create a magic link for them
        token = create_magic_link_token(email)
        verify_url = f"{base_url}/auth/verify?token={token}"

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 20px;">
<tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:440px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
  <tr><td style="background:#1a1d27;padding:32px 24px;text-align:center;">
    <div style="font-size:32px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">
      <span style="color:#4ecdc4;">H</span>uddle
    </div>
    <div style="color:#888;font-size:14px;margin-top:4px;">Your household, sorted.</div>
  </td></tr>
  <tr><td style="padding:32px 28px;">
    <h1 style="margin:0 0 8px;font-size:20px;font-weight:700;color:#1a1d27;">You've been invited!</h1>
    <p style="margin:0 0 24px;color:#555;font-size:15px;line-height:1.5;">
      Hey {name}! You've been added to <strong>{household_name}</strong> on Huddle.
      Tap the button below to set up your account and start using the app.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center">
        <a href="{verify_url}"
           style="display:inline-block;background:#4ecdc4;color:#1a1d27;padding:14px 40px;border-radius:12px;font-size:16px;font-weight:700;text-decoration:none;letter-spacing:0.3px;">
          Join {household_name}
        </a>
      </td></tr>
    </table>
    <p style="margin:24px 0 0;color:#999;font-size:13px;line-height:1.5;">
      This link expires in <strong>30 minutes</strong>. You can always request a new one at
      <a href="{base_url}/onboard" style="color:#4ecdc4;">huddle.rodgersgroup.au</a>.
    </p>
  </td></tr>
  <tr><td style="border-top:1px solid #eee;padding:20px 28px;text-align:center;">
    <p style="margin:0;color:#bbb;font-size:12px;">
      Can't click the button? Copy this link:<br>
      <a href="{verify_url}" style="color:#4ecdc4;word-break:break-all;font-size:11px;">{verify_url}</a>
    </p>
  </td></tr>
</table>
<p style="margin:20px 0 0;color:#bbb;font-size:11px;text-align:center;">
  Huddle &mdash; huddle.rodgersgroup.au
</p>
</td></tr>
</table>
</body>
</html>"""

        resend.Emails.send({
            "from": "Huddle <noreply@huddle.rodgersgroup.au>",
            "to": [email],
            "subject": f"You've been invited to {household_name} on Huddle!",
            "html": html,
        })
        logger.info("Invite email sent to %s (%s) for household '%s'", name, email, household_name)
    except Exception as e:
        logger.warning("Failed to send invite email to %s: %s", email, e)


def _notify_superadmin_household_event(event_type: str, household_name: str, user_name: str):
    """Notify superadmin via email when a household is created or a user joins.

    Args:
        event_type: 'created' or 'joined'
        household_name: Name of the household
        user_name: Display name of the user who created/joined
    """
    try:
        import resend
        from config import RESEND_API_KEY

        if not RESEND_API_KEY:
            return

        resend.api_key = RESEND_API_KEY

        if event_type == "created":
            subject = f"[Huddle] New household: {household_name}"
            heading = "New Household Created"
            body_text = f"<strong>{user_name}</strong> created a new household called <strong>{household_name}</strong>."
        else:
            subject = f"[Huddle] {user_name} joined {household_name}"
            heading = "New Member Joined"
            body_text = f"<strong>{user_name}</strong> joined household <strong>{household_name}</strong>."

        resend.Emails.send({
            "from": "Huddle <noreply@huddle.rodgersgroup.au>",
            "to": ["keiran@rodgersgroup.au"],
            "subject": subject,
            "html": (
                f'<div style="font-family:-apple-system,sans-serif;max-width:440px;margin:0 auto;padding:20px;">'
                f'<div style="background:#1a1d27;padding:20px;border-radius:12px 12px 0 0;text-align:center;">'
                f'<span style="font-size:24px;font-weight:800;color:#fff;"><span style="color:#4ecdc4;">H</span>uddle</span></div>'
                f'<div style="background:#fff;padding:24px;border:1px solid #eee;border-radius:0 0 12px 12px;">'
                f'<h2 style="margin:0 0 12px;font-size:18px;color:#1a1d27;">{heading}</h2>'
                f'<p style="color:#555;line-height:1.5;">{body_text}</p>'
                f'<p style="margin-top:16px;"><a href="https://huddle.rodgersgroup.au/superadmin" '
                f'style="color:#4ecdc4;font-weight:600;">View in Super Admin</a></p>'
                f'</div></div>'
            ),
        })
        logger.info("Superadmin notified: %s %s by %s", event_type, household_name, user_name)
    except Exception as e:
        logger.warning("Failed to send household event email: %s", e)


def create_magic_link_token(email: str) -> str:
    """Create a magic link token for the given email address."""
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(seconds=MAGIC_LINK_MAX_AGE)).isoformat()

    with get_db() as conn:
        conn.execute(
            "UPDATE magic_links SET used = 1 WHERE email = ? AND used = 0",
            (email,),
        )
        conn.execute(
            """INSERT INTO magic_links (token, email, created_at, expires_at, used)
               VALUES (?, ?, ?, ?, 0)""",
            (token, email, now, expires_at),
        )
        conn.commit()

    return token


def verify_magic_link_token(token: str):
    """Verify a magic link token. Returns email or None."""
    now = datetime.now().isoformat()

    with get_db() as conn:
        row = conn.execute(
            """SELECT id, email, expires_at, used
               FROM magic_links
               WHERE token = ?""",
            (token,),
        ).fetchone()

        if not row:
            return None
        if row["used"]:
            return None
        if row["expires_at"] < now:
            return None

        conn.execute(
            "UPDATE magic_links SET used = 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        return row["email"]


def get_or_create_user(email: str, display_name: str = None) -> int:
    """Look up an existing user by email or create a new one. Returns user_id."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            return existing["id"]

        if not display_name:
            display_name = email.split("@")[0].title()

        now = datetime.now().isoformat()
        cursor = conn.execute(
            "INSERT INTO users (email, display_name, created_at) VALUES (?, ?, ?)",
            (email, display_name, now),
        )
        conn.commit()
        return cursor.lastrowid


def generate_invite_code() -> str:
    """Generate an 8-character uppercase alphanumeric invite code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# FastAPI dependencies


def _set_sentry_context(tenant: "TenantContext"):
    """Attach tenant info to Sentry scope (no-op if Sentry not installed)."""
    try:
        import sentry_sdk
        sentry_sdk.set_user({"id": str(tenant.user_id), "username": tenant.display_name})
        sentry_sdk.set_tag("household_id", tenant.household_id)
    except (ImportError, AttributeError):
        pass


async def get_current_user(request: Request) -> TenantContext:
    """FastAPI dependency that extracts and validates the session."""
    # Check session cookie
    token = request.cookies.get(COOKIE_NAME)
    if token:
        data = verify_session_token(token)
        if data:
            # User authenticated but not yet in a household
            if data["household_id"] == 0:
                with get_db() as conn:
                    user = conn.execute(
                        "SELECT id, display_name FROM users WHERE id = ?",
                        (data["user_id"],),
                    ).fetchone()
                    if user:
                        tenant = TenantContext(
                            user_id=user["id"],
                            household_id=0,
                            display_name=user["display_name"] or "New User",
                            role="pending",
                        )
                        _set_sentry_context(tenant)
                        return tenant
            else:
                with get_db() as conn:
                    member = conn.execute(
                        """
                        SELECT hm.display_name, hm.role, hm.household_id, hm.user_id
                        FROM household_members hm
                        WHERE hm.user_id = ? AND hm.household_id = ?
                        """,
                        (data["user_id"], data["household_id"]),
                    ).fetchone()
                    if member:
                        tenant = TenantContext(
                            user_id=member["user_id"],
                            household_id=member["household_id"],
                            display_name=member["display_name"],
                            role=member["role"],
                            original_household_id=data.get("original_household_id"),
                        )
                        _set_sentry_context(tenant)
                        return tenant
                    # Superadmin impersonating a household they're not a member of
                    if data.get("original_household_id") is not None:
                        user = conn.execute(
                            "SELECT id, display_name FROM users WHERE id = ?",
                            (data["user_id"],),
                        ).fetchone()
                        if user:
                            tenant = TenantContext(
                                user_id=user["id"],
                                household_id=data["household_id"],
                                display_name=user["display_name"] or "Superadmin",
                                role="manager",
                                original_household_id=data["original_household_id"],
                            )
                            _set_sentry_context(tenant)
                            return tenant

    # Check Bearer token — could be an access token (AnyList-style) or a kiosk token
    auth_header = request.headers.get("Authorization", "")
    bearer_token = None
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]

    if bearer_token:
        # Try as access token first
        data = verify_access_token(bearer_token)
        if data:
            with get_db() as conn:
                # Update last_used_at (throttled: only if >5 min stale)
                session_id = data.get("session_id")
                if session_id:
                    _touch_session(conn, session_id)

                if data["household_id"] == 0:
                    user = conn.execute(
                        "SELECT id, display_name FROM users WHERE id = ?",
                        (data["user_id"],),
                    ).fetchone()
                    if user:
                        tenant = TenantContext(
                            user_id=user["id"],
                            household_id=0,
                            display_name=user["display_name"] or "New User",
                            role="pending",
                        )
                        _set_sentry_context(tenant)
                        return tenant
                else:
                    member = conn.execute(
                        """SELECT hm.display_name, hm.role, hm.household_id, hm.user_id
                           FROM household_members hm
                           WHERE hm.user_id = ? AND hm.household_id = ?""",
                        (data["user_id"], data["household_id"]),
                    ).fetchone()
                    if member:
                        tenant = TenantContext(
                            user_id=member["user_id"],
                            household_id=member["household_id"],
                            display_name=member["display_name"],
                            role=member["role"],
                        )
                        _set_sentry_context(tenant)
                        return tenant

        # Try as kiosk token
        with get_db() as conn:
            kt = conn.execute(
                "SELECT * FROM kiosk_tokens WHERE token = ?", (bearer_token,)
            ).fetchone()
            if kt:
                conn.execute(
                    "UPDATE kiosk_tokens SET last_used_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), kt["id"]),
                )
                conn.commit()
                tenant = TenantContext(
                    user_id=0,
                    household_id=kt["household_id"],
                    display_name="Kiosk",
                    role="kiosk",
                )
                _set_sentry_context(tenant)
                return tenant

    # Fallback: kiosk token via query param (legacy)
    kiosk_query_token = request.query_params.get("token")
    if kiosk_query_token:
        with get_db() as conn:
            kt = conn.execute(
                "SELECT * FROM kiosk_tokens WHERE token = ?", (kiosk_query_token,)
            ).fetchone()
            if kt:
                conn.execute(
                    "UPDATE kiosk_tokens SET last_used_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), kt["id"]),
                )
                conn.commit()
                tenant = TenantContext(
                    user_id=0,
                    household_id=kt["household_id"],
                    display_name="Kiosk",
                    role="kiosk",
                )
                _set_sentry_context(tenant)
                return tenant

    raise HTTPException(status_code=401, detail="Not authenticated")


async def get_optional_user(request: Request):
    """Returns TenantContext or None -- for pages that work with or without auth."""
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


# Router

router = APIRouter()


# ---------------------------------------------------------------------------
# AnyList-style email + password auth endpoints
# ---------------------------------------------------------------------------

@router.post("/api/auth/signup")
async def signup(request: Request):
    """Create a new account with email + password. Returns access/refresh tokens."""
    if not BCRYPT_ENABLED:
        raise HTTPException(status_code=500, detail="Password auth not available (bcrypt not installed)")

    client_ip = request.client.host if request.client else "unknown"
    if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH, window_seconds=600):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    display_name = body.get("display_name", "").strip()
    device_id = body.get("device_id")
    device_name = body.get("device_name") or _parse_user_agent(request.headers.get("User-Agent", ""))

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if not display_name:
        display_name = email.split("@")[0]

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="An account with this email already exists")

        pw_hash = hash_password(password)
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO users (email, display_name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (email, display_name, pw_hash, now),
        )
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

    # New user — household_id=0 means not yet in a household
    tokens = create_session_with_tokens(
        user_id=user["id"],
        household_id=0,
        device_id=device_id,
        device_name=device_name,
        ip_address=client_ip,
    )
    tokens["display_name"] = display_name
    return JSONResponse(content=tokens)


@router.post("/api/auth/token")
async def login_token(request: Request):
    """Login with email + password. Returns access/refresh tokens (AnyList-style)."""
    if not BCRYPT_ENABLED:
        raise HTTPException(status_code=500, detail="Password auth not available")

    client_ip = request.client.host if request.client else "unknown"
    if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH, window_seconds=600):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    device_id = body.get("device_id")
    device_name = body.get("device_name") or _parse_user_agent(request.headers.get("User-Agent", ""))

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, email, display_name, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if not user or not user["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Find user's household (pick the first one, or 0 if not in any)
    with get_db() as conn:
        membership = conn.execute(
            "SELECT household_id FROM household_members WHERE user_id = ? LIMIT 1",
            (user["id"],),
        ).fetchone()
    household_id = membership["household_id"] if membership else 0

    tokens = create_session_with_tokens(
        user_id=user["id"],
        household_id=household_id,
        device_id=device_id,
        device_name=device_name,
        ip_address=client_ip,
    )
    tokens["display_name"] = user["display_name"]
    return JSONResponse(content=tokens)


@router.post("/api/auth/password-login")
async def password_login_cookie(request: Request):
    """Login with email + password and set a session cookie (for the onboard page).

    Unlike /api/auth/token which returns bearer tokens, this sets the same
    cookie that magic-link auth uses, so /mobile page checks work seamlessly.
    """
    if not BCRYPT_ENABLED:
        raise HTTPException(status_code=500, detail="Password auth not available")

    client_ip = request.client.host if request.client else "unknown"
    if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH, window_seconds=600):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, email, display_name, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if not user or not user["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    with get_db() as conn:
        membership = conn.execute(
            "SELECT household_id FROM household_members WHERE user_id = ? LIMIT 1",
            (user["id"],),
        ).fetchone()
    household_id = membership["household_id"] if membership else 0

    session_token = create_session_token(user["id"], household_id)
    redirect_url = "/mobile" if membership else "/onboard"

    response = JSONResponse(content={"ok": True, "redirect": redirect_url})
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@router.post("/api/auth/token/refresh")
async def refresh_token(request: Request):
    """Exchange a refresh token for a new access/refresh token pair (token rotation)."""
    client_ip = request.client.host if request.client else "unknown"
    if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH * 2, window_seconds=600):
        return JSONResponse(status_code=429, content={"error": "Too many requests"})

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    refresh = body.get("refresh_token", "")
    if not refresh:
        raise HTTPException(status_code=400, detail="refresh_token is required")

    tokens = refresh_session_tokens(refresh, ip_address=client_ip)
    return JSONResponse(content=tokens)


@router.post("/api/auth/magic-link")
async def request_magic_link(request: Request):
    """Send a magic link email to the given address."""
    try:
        client_ip = request.client.host if request.client else "unknown"
        if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH, window_seconds=600):
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests. Please wait a few minutes and try again."},
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        email = body.get("email", "").strip().lower()

        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="A valid email address is required")

        token = create_magic_link_token(email)

        base_url = str(request.base_url).rstrip("/")
        verify_url = f"{base_url}/auth/verify?token={token}"

        from config import RESEND_API_KEY
        resend_api_key = RESEND_API_KEY or get_setting("resend_api_key", default="")
        if EMAIL_ENABLED and resend_api_key:
            resend.api_key = resend_api_key
            resend.Emails.send({
                "from": "Huddle <noreply@huddle.rodgersgroup.au>",
                "to": [email],
                "subject": "Your Huddle login link",
                "html": _build_magic_link_email(verify_url),
            })
        else:
            logger.info("MAGIC LINK for %s: %s", email, verify_url)

        return {"ok": True, "message": "If that email is valid, a login link has been sent."}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in request_magic_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in request_magic_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/auth/verify")
async def verify_magic_link(request: Request, token: str):
    """Verify a magic link token from the user's email."""
    try:
        client_ip = request.client.host if request.client else "unknown"
        if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH * 2, window_seconds=600):
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests. Please wait a few minutes and try again."},
            )

        email = verify_magic_link_token(token)
        if not email:
            redirect_url = request.query_params.get("redirect", "/onboard")
            if not redirect_url.startswith("/"):
                redirect_url = "/onboard"
            return RedirectResponse(url=f"{redirect_url}?error=expired", status_code=302)

        user_id = get_or_create_user(email)

        with get_db() as conn:
            # Prefer the most recently joined household (latest joined_at)
            membership = conn.execute(
                """SELECT hm.household_id, hm.display_name, hm.role
                   FROM household_members hm
                   WHERE hm.user_id = ?
                   ORDER BY hm.joined_at DESC
                   LIMIT 1""",
                (user_id,),
            ).fetchone()

        client_ip = request.client.host if request.client else "unknown"
        device_name = _parse_user_agent(request.headers.get("User-Agent", ""))
        household_id = membership["household_id"] if membership else 0

        # Create a sessions table row so magic-link sessions appear in device management
        tokens = create_session_with_tokens(
            user_id=user_id,
            household_id=household_id,
            device_name=device_name,
            ip_address=client_ip,
        )

        # Also set cookie for the legacy cookie auth path
        session_token = create_session_token(user_id, household_id)

        if membership:
            redirect_url = request.query_params.get("redirect", "/mobile")
            if not redirect_url.startswith("/"):
                redirect_url = "/mobile"
        else:
            redirect_url = request.query_params.get("redirect", "/onboard")
            if not redirect_url.startswith("/"):
                redirect_url = "/onboard"

        response = RedirectResponse(url=redirect_url, status_code=303)
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in verify_magic_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in verify_magic_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/auth/logout")
async def logout(request: Request):
    """Logout: revoke token session (if Bearer auth) and clear cookie."""
    # Revoke token-based session if present
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        data = verify_access_token(auth_header[7:])
        if data and "session_id" in data:
            revoke_session(data["session_id"])

    response = Response(
        content=json.dumps({"ok": True, "message": "Logged out"}),
        media_type="application/json",
    )
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@router.post("/api/auth/password/set")
async def set_password(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Set a password for a magic-link-only account. Rejects if user already has one."""
    if not BCRYPT_ENABLED:
        raise HTTPException(status_code=500, detail="Password auth not available")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    password = body.get("password", "")
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE id = ?", (tenant.user_id,)
        ).fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["password_hash"]:
        raise HTTPException(status_code=409, detail="Account already has a password. Use /api/auth/password/change instead.")

    new_hash = hash_password(password)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, tenant.user_id),
        )
        conn.commit()

    logger.info("Password set for magic-link user %s", tenant.user_id)
    return {"ok": True, "message": "Password set successfully. You can now log in with email and password."}


@router.post("/api/auth/password/change")
async def change_password(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Change the current user's password. Revokes all other sessions for security."""
    if not BCRYPT_ENABLED:
        raise HTTPException(status_code=500, detail="Password auth not available")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")

    if not current_password or not new_password:
        raise HTTPException(status_code=400, detail="Both current_password and new_password are required")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters")

    with get_db() as conn:
        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE id = ?", (tenant.user_id,)
        ).fetchone()

    if not user or not user["password_hash"]:
        raise HTTPException(status_code=400, detail="Account does not use password authentication")

    if not verify_password(current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = hash_password(new_password)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, tenant.user_id),
        )
        conn.commit()

    # Revoke all other sessions (keep current one)
    auth_header = request.headers.get("Authorization", "")
    current_session_id = None
    if auth_header.startswith("Bearer "):
        data = verify_access_token(auth_header[7:])
        if data:
            current_session_id = data.get("session_id")
    revoke_all_sessions(tenant.user_id, except_session_id=current_session_id)

    logger.info("Password changed for user %s, other sessions revoked", tenant.user_id)
    return {"ok": True, "message": "Password changed. All other sessions have been logged out."}


@router.get("/api/auth/me")
async def get_me(user: TenantContext = Depends(get_current_user)):
    """Return the current user's info, their household, and the household's members."""
    try:
        with get_db() as conn:
            household = conn.execute(
                "SELECT * FROM households WHERE id = ?", (user.household_id,)
            ).fetchone()

            members = conn.execute(
                """SELECT hm.user_id, hm.display_name, hm.role, hm.color,
                          u.email
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ?
                   ORDER BY hm.display_name""",
                (user.household_id,),
            ).fetchall()

            # Fetch all households this user belongs to
            all_households = conn.execute(
                """SELECT h.id, h.name, hm.role, hm.display_name
                   FROM household_members hm
                   JOIN households h ON h.id = hm.household_id
                   WHERE hm.user_id = ?
                   ORDER BY h.name""",
                (user.user_id,),
            ).fetchall()

            # Get current user's email and password status from users table
            current_user_row = conn.execute(
                "SELECT email, password_hash FROM users WHERE id = ?", (user.user_id,)
            ).fetchone()
            current_user_email = current_user_row["email"] if current_user_row else None
            has_password = bool(current_user_row["password_hash"]) if current_user_row else False

        # Check if current user is superadmin
        from config import SUPERADMIN_EMAILS
        is_superadmin = current_user_email in SUPERADMIN_EMAILS

        result_user = {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "email": current_user_email,
            "role": user.role,
            "avatar_url": get_avatar_url(user.user_id),
            "is_superadmin": is_superadmin,
            "has_password": has_password,
        }
        result = {
            "user": result_user,
            "household": {
                "id": household["id"],
                "name": household["name"],
                "invite_code": household["invite_code"] if user.role == "manager" else None,
            } if household else None,
            "households": [
                {
                    "id": h["id"],
                    "name": h["name"],
                    "role": h["role"],
                    "display_name": h["display_name"],
                    "is_current": h["id"] == user.household_id,
                }
                for h in all_households
            ],
            "members": [
                {
                    "user_id": m["user_id"],
                    "display_name": m["display_name"],
                    "role": m["role"],
                    "color": m["color"],
                    "email": m["email"],
                    "avatar_url": get_avatar_url(m["user_id"]),
                }
                for m in members
            ],
        }
        if user.original_household_id is not None:
            result["original_household_id"] = user.original_household_id
        return result
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_me: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_me: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/auth/households")
async def list_user_households(user: TenantContext = Depends(get_current_user)):
    """List all households the current user belongs to."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT h.id, h.name, hm.role, hm.display_name
                   FROM household_members hm
                   JOIN households h ON h.id = hm.household_id
                   WHERE hm.user_id = ?
                   ORDER BY h.name""",
                (user.user_id,),
            ).fetchall()

        return {
            "households": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "role": r["role"],
                    "display_name": r["display_name"],
                    "is_current": r["id"] == user.household_id,
                }
                for r in rows
            ]
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_user_households: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_user_households: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/switch")
async def switch_household(request: Request, user: TenantContext = Depends(get_current_user)):
    """Switch the current session to a different household."""
    try:
        body = await request.json()
        target_id = body.get("household_id")
        if not target_id:
            raise HTTPException(status_code=400, detail="household_id is required")

        with get_db() as conn:
            membership = conn.execute(
                """SELECT hm.household_id, hm.display_name, h.name as household_name
                   FROM household_members hm
                   JOIN households h ON h.id = hm.household_id
                   WHERE hm.user_id = ? AND hm.household_id = ?""",
                (user.user_id, target_id),
            ).fetchone()

        if not membership:
            raise HTTPException(status_code=403, detail="You are not a member of that household")

        session_token = create_session_token(user.user_id, target_id)
        response = Response(
            content=json.dumps({
                "ok": True,
                "household": {
                    "id": membership["household_id"],
                    "name": membership["household_name"],
                },
            }),
            media_type="application/json",
        )
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in switch_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in switch_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/household/invite-link")
async def get_invite_link(request: Request, user: TenantContext = Depends(get_current_user)):
    """Get the invite code and full shareable URL for the current household."""
    try:
        with get_db() as conn:
            household = conn.execute(
                "SELECT invite_code, name FROM households WHERE id = ?",
                (user.household_id,),
            ).fetchone()

        if not household or not household["invite_code"]:
            raise HTTPException(status_code=404, detail="No invite code found")

        base_url = str(request.base_url).rstrip("/")
        invite_code = household["invite_code"]

        return {
            "invite_code": invite_code,
            "invite_url": f"{base_url}/onboard?invite={invite_code}",
            "household_name": household["name"],
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_invite_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_invite_link: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/create")
async def create_household(request: Request):
    """Create a new household and add the current user as its owner."""
    try:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        data = verify_session_token(token)
        if not data:
            raise HTTPException(status_code=401, detail="Invalid session")

        user_id = data["user_id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        household_name = body.get("name", "").strip()
        display_name = body.get("display_name", "").strip()
        color = body.get("color", "#4ecdc4").strip()
        household_type = body.get("household_type", "family").strip()
        if household_type not in ("family", "sharehouse", "student_house", "couple", "solo", "other"):
            household_type = "family"
        segment = body.get("segment", "").strip()
        if segment not in ("solo", "household", "sharehouse"):
            # Derive segment from household_type if not provided
            segment_map = {"solo": "solo", "couple": "solo", "sharehouse": "sharehouse",
                           "student_house": "sharehouse", "family": "household", "other": "household"}
            segment = segment_map.get(household_type, "household")

        if not household_name:
            raise HTTPException(status_code=400, detail="Household name is required")
        if not display_name:
            raise HTTPException(status_code=400, detail="Your display name is required")

        now = datetime.now().isoformat()
        invite_code = generate_invite_code()

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO households (name, invite_code, created_by, created_at, household_type, segment)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (household_name, invite_code, user_id, now, household_type, segment),
            )
            household_id = cursor.lastrowid

            conn.execute(
                """INSERT INTO household_members
                   (household_id, user_id, display_name, role, color, joined_at)
                   VALUES (?, ?, ?, 'manager', ?, ?)""",
                (household_id, user_id, display_name, color, now),
            )
            conn.commit()

        session_token = create_session_token(user_id, household_id)
        response = Response(
            content=json.dumps({
                "ok": True,
                "household": {
                    "id": household_id,
                    "name": household_name,
                    "invite_code": invite_code,
                },
            }),
            media_type="application/json",
        )
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )

        # Notify superadmin
        _notify_superadmin_household_event("created", household_name, display_name)

        return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/join")
async def join_household(request: Request):
    """Join an existing household using an invite code."""
    try:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        data = verify_session_token(token)
        if not data:
            raise HTTPException(status_code=401, detail="Invalid session")

        user_id = data["user_id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        invite_code = body.get("invite_code", "").strip().upper()
        display_name = body.get("display_name", "").strip()
        color = body.get("color", "#ff6b9d").strip()
        password = body.get("password", "").strip()

        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required")
        if not display_name:
            raise HTTPException(status_code=400, detail="Your display name is required")

        now = datetime.now().isoformat()

        with get_db() as conn:
            household = conn.execute(
                "SELECT id, name FROM households WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Invalid invite code")

            household_id = household["id"]

            existing = conn.execute(
                "SELECT id FROM household_members WHERE user_id = ? AND household_id = ?",
                (user_id, household_id),
            ).fetchone()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail="You are already a member of this household",
                )

            # Check if a placeholder member with this display name exists
            # (created during onboarding before the real user signed up)
            existing_name = conn.execute(
                """SELECT hm.id, hm.user_id, u.email
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ? AND hm.display_name = ?""",
                (household_id, display_name),
            ).fetchone()

            if existing_name:
                if existing_name["email"].endswith("@placeholder"):
                    # Claim the placeholder member record
                    conn.execute(
                        "UPDATE household_members SET user_id = ?, color = ?, joined_at = ? WHERE id = ?",
                        (user_id, color, now, existing_name["id"]),
                    )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail=f"A member named '{display_name}' already exists in this household",
                    )
            else:
                conn.execute(
                    """INSERT INTO household_members
                       (household_id, user_id, display_name, role, color, joined_at)
                       VALUES (?, ?, ?, 'member', ?, ?)""",
                    (household_id, user_id, display_name, color, now),
                )
            conn.commit()

            # Set password if provided during join (optional, >= MIN_PASSWORD_LENGTH chars)
            if password and len(password) >= MIN_PASSWORD_LENGTH and BCRYPT_ENABLED:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash IS NULL",
                    (hash_password(password), user_id),
                )
                conn.commit()

        session_token = create_session_token(user_id, household_id)
        response = Response(
            content=json.dumps({
                "ok": True,
                "household": {
                    "id": household_id,
                    "name": household["name"],
                },
            }),
            media_type="application/json",
        )
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )

        # Notify superadmin
        _notify_superadmin_household_event("joined", household["name"], display_name)

        return response
    except HTTPException:
        raise
    except sqlite3.IntegrityError as e:
        if "household_members.household_id, household_members.display_name" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"A member named '{display_name}' already exists in this household",
            )
        logger.error("Database error in join_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except sqlite3.Error as e:
        logger.error("Database error in join_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in join_household: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/add-members")
async def add_members(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Add members to the household during onboarding. If email is provided, uses real email and sends invite."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can add members")

        body = await request.json()
        members = body.get("members", [])

        now = datetime.now().isoformat()
        colors = ["#ff6b9d", "#4caf50", "#ff9800", "#9c27b0", "#2196f3",
                  "#e91e63", "#00bcd4", "#ffeb3b", "#8bc34a", "#ff5722"]

        invite_emails = []  # Collect (email, name) pairs to send invites after commit

        with get_db() as conn:
            # Check manager limit
            if any(m.get("role") == "manager" for m in members):
                existing_managers = conn.execute(
                    "SELECT COUNT(*) as c FROM household_members WHERE household_id = ? AND role = 'manager'",
                    (user.household_id,),
                ).fetchone()["c"]
                new_managers = sum(1 for m in members if m.get("role") == "manager")
                if existing_managers + new_managers > 5:
                    raise HTTPException(status_code=400, detail="Maximum 5 managers per household")

            existing_count = conn.execute(
                "SELECT COUNT(*) as c FROM household_members WHERE household_id = ?",
                (user.household_id,),
            ).fetchone()["c"]

            # Get household name for invite emails
            hh_row = conn.execute("SELECT name FROM households WHERE id = ?", (user.household_id,)).fetchone()
            household_name = hh_row["name"] if hh_row else "your household"

            for i, member in enumerate(members):
                name = member.get("name", "").strip()
                role = member.get("role", "member")
                email = member.get("email", "").strip().lower() if isinstance(member.get("email"), str) else ""
                if not name:
                    continue
                if role not in ("manager", "member", "dependent"):
                    role = "member"

                # Use real email if provided, otherwise placeholder
                if email and "@" in email and "@placeholder" not in email:
                    # Check if a user with this email already exists
                    existing_user = conn.execute(
                        "SELECT id FROM users WHERE email = ?", (email,)
                    ).fetchone()
                    if existing_user:
                        new_user_id = existing_user["id"]
                    else:
                        cursor = conn.execute(
                            "INSERT INTO users (email, display_name, created_at) VALUES (?, ?, ?)",
                            (email, name, now),
                        )
                        new_user_id = cursor.lastrowid
                    invite_emails.append((email, name, household_name))
                else:
                    placeholder_email = f"placeholder_{secrets.token_hex(4)}@placeholder"
                    cursor = conn.execute(
                        "INSERT INTO users (email, display_name, created_at) VALUES (?, ?, ?)",
                        (placeholder_email, name, now),
                    )
                    new_user_id = cursor.lastrowid

                color = colors[(existing_count + i) % len(colors)]

                conn.execute(
                    """INSERT INTO household_members
                       (household_id, user_id, display_name, role, color, joined_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user.household_id, new_user_id, name, role, color, now),
                )
            conn.commit()

        # Send invite emails outside the DB transaction
        base_url = str(request.base_url).rstrip("/")
        invited_count = 0
        for email, name, hh_name in invite_emails:
            try:
                _send_member_invite_email(email, name, hh_name, base_url)
                invited_count += 1
            except Exception as e:
                logger.warning("Failed to send invite to %s: %s", email, e)

        return {"ok": True, "invited": invited_count}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in add_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in add_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/household/members")
async def list_members(user: TenantContext = Depends(get_current_user)):
    """Return all members of the current user's household."""
    try:
        with get_db() as conn:
            members = conn.execute(
                """SELECT hm.user_id, hm.display_name, hm.role, hm.color, hm.joined_at
                   FROM household_members hm
                   WHERE hm.household_id = ?
                   ORDER BY hm.joined_at""",
                (user.household_id,),
            ).fetchall()

        return {
            "members": [
                {
                    "user_id": m["user_id"],
                    "display_name": m["display_name"],
                    "role": m["role"],
                    "color": m["color"],
                    "joined_at": m["joined_at"],
                }
                for m in members
            ]
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/invite")
async def generate_invite(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Generate (or regenerate) an invite code for the household."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only owners and admins can generate invites")

        new_code = generate_invite_code()

        with get_db() as conn:
            conn.execute(
                "UPDATE households SET invite_code = ? WHERE id = ?",
                (new_code, user.household_id),
            )
            conn.commit()

        base_url = str(request.base_url).rstrip("/")
        invite_url = f"{base_url}/onboard?invite={new_code}"

        return {
            "ok": True,
            "invite_code": new_code,
            "invite_url": invite_url,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in generate_invite: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in generate_invite: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/household/kiosk-token")
async def create_kiosk_token(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Generate a new kiosk token for the household."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only owners and admins can create kiosk tokens")

        body = await request.json()
        label = body.get("label", "Kiosk").strip()

        token_value = secrets.token_urlsafe(48)
        now = datetime.now().isoformat()

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO kiosk_tokens (household_id, token, label, created_at)
                   VALUES (?, ?, ?, ?)""",
                (user.household_id, token_value, label, now),
            )
            conn.commit()
            token_id = cursor.lastrowid

        return {
            "ok": True,
            "kiosk_token": {
                "id": token_id,
                "token": token_value,
                "label": label,
                "created_at": now,
            },
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_kiosk_token: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_kiosk_token: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/household/kiosk-tokens")
async def list_kiosk_tokens(user: TenantContext = Depends(get_current_user)):
    """List all kiosk tokens for the household."""
    try:

        with get_db() as conn:
            tokens = conn.execute(
                """SELECT id, label, created_at, last_used_at
                   FROM kiosk_tokens
                   WHERE household_id = ?
                   ORDER BY created_at""",
                (user.household_id,),
            ).fetchall()

        return {
            "kiosk_tokens": [
                {
                    "id": t["id"],
                    "label": t["label"],
                    "created_at": t["created_at"],
                    "last_used_at": t["last_used_at"],
                }
                for t in tokens
            ]
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in list_kiosk_tokens: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in list_kiosk_tokens: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/household/member/{member_name}")
async def remove_member(
    member_name: str,
    user: TenantContext = Depends(get_current_user),
):
    """Remove a household member. Manager only."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can remove members")

        with get_db() as conn:
            # Don't allow removing yourself
            member = conn.execute(
                "SELECT hm.user_id FROM household_members hm WHERE hm.household_id = ? AND hm.display_name = ?",
                (user.household_id, member_name),
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")
            if member["user_id"] == user.user_id:
                raise HTTPException(status_code=400, detail="Cannot remove yourself")

            conn.execute(
                "DELETE FROM household_members WHERE household_id = ? AND display_name = ?",
                (user.household_id, member_name),
            )
            conn.commit()

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error removing member: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/household/member/{member_name}/color")
async def update_member_color(
    member_name: str,
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Update a household member's color."""
    try:
        body = await request.json()
        color = body.get("color", "").strip()
        if not color or not color.startswith("#"):
            raise HTTPException(status_code=400, detail="Invalid color")

        with get_db() as conn:
            result = conn.execute(
                "UPDATE household_members SET color = ? WHERE household_id = ? AND display_name = ?",
                (color, user.household_id, member_name),
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Member not found")

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error updating member color: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/household/kiosk-token/{token_id}")
async def revoke_kiosk_token(
    token_id: int,
    user: TenantContext = Depends(get_current_user),
):
    """Revoke (delete) a kiosk token."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only owners and admins can revoke kiosk tokens")

        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM kiosk_tokens WHERE id = ? AND household_id = ?",
                (token_id, user.household_id),
            )
            conn.commit()

            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Kiosk token not found")

        return {"ok": True, "message": "Kiosk token revoked"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in revoke_kiosk_token: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in revoke_kiosk_token: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/household/plan")
async def get_household_plan(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Get current segment, tier, available tiers, and member limits."""
    from tiers import SEGMENTS, TIERS, get_max_members
    try:
        with get_db() as conn:
            h_row = conn.execute(
                "SELECT segment, tier FROM households WHERE id = ?",
                (user.household_id,)
            ).fetchone()
            segment = h_row["segment"] if h_row and h_row["segment"] else "household"
            tier = h_row["tier"] if h_row and h_row["tier"] else "free"

            member_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM household_members WHERE household_id = ?",
                (user.household_id,)
            ).fetchone()["cnt"]

        seg_info = SEGMENTS.get(segment, SEGMENTS["household"])
        tier_info = TIERS.get(tier, TIERS["free"])

        available_tiers = []
        for t_code in seg_info["tiers"]:
            t_info = TIERS.get(t_code, {})
            available_tiers.append({
                "code": t_code,
                "name": t_info.get("name", t_code),
                "price_monthly": t_info.get("price_monthly", 0),
                "price_annual": t_info.get("price_annual", 0),
                "max_members": get_max_members(segment, t_code),
                "current": t_code == tier,
            })

        available_segments = []
        for s_code, s_info in SEGMENTS.items():
            available_segments.append({
                "code": s_code,
                "name": s_info["name"],
                "description": s_info["description"],
                "current": s_code == segment,
            })

        return {
            "segment": segment,
            "segment_name": seg_info["name"],
            "tier": tier,
            "tier_name": tier_info.get("name", "Free"),
            "price_monthly": tier_info.get("price_monthly", 0),
            "price_annual": tier_info.get("price_annual", 0),
            "max_members": get_max_members(segment, tier),
            "current_members": member_count,
            "available_tiers": available_tiers,
            "available_segments": available_segments,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_household_plan: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_household_plan: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/household/segment")
async def update_household_segment(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Switch the household's segment (manager only). Resets tier to free."""
    from tiers import SEGMENTS
    from websocket import manager as ws_manager
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can change the segment")

        body = await request.json()
        new_segment = body.get("segment", "").strip()
        if new_segment not in SEGMENTS:
            raise HTTPException(status_code=400, detail=f"Invalid segment: {new_segment}")

        with get_db() as conn:
            h_row = conn.execute(
                "SELECT segment FROM households WHERE id = ?",
                (user.household_id,)
            ).fetchone()
            old_segment = h_row["segment"] if h_row else "household"

            conn.execute(
                "UPDATE households SET segment = ?, tier = 'free' WHERE id = ?",
                (new_segment, user.household_id)
            )
            conn.commit()

        logger.info("Household %d changed segment from '%s' to '%s'",
                     user.household_id, old_segment, new_segment)

        await ws_manager.broadcast({"type": "settings_updated"}, household_id=user.household_id)
        return {"ok": True, "segment": new_segment, "tier": "free"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_household_segment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_household_segment: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/household/tier")
async def update_household_tier(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Switch the household's tier within its current segment (manager only)."""
    from tiers import is_tier_valid_for_segment, get_max_members
    from websocket import manager as ws_manager
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can change the tier")

        body = await request.json()
        new_tier = body.get("tier", "").strip()

        with get_db() as conn:
            h_row = conn.execute(
                "SELECT segment, tier FROM households WHERE id = ?",
                (user.household_id,)
            ).fetchone()
            segment = h_row["segment"] if h_row else "household"

            if not is_tier_valid_for_segment(segment, new_tier):
                raise HTTPException(
                    status_code=400,
                    detail=f"Tier '{new_tier}' is not valid for segment '{segment}'"
                )

            conn.execute(
                "UPDATE households SET tier = ? WHERE id = ?",
                (new_tier, user.household_id)
            )
            conn.commit()

            # Warn if member count exceeds new tier limit
            member_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM household_members WHERE household_id = ?",
                (user.household_id,)
            ).fetchone()["cnt"]
            max_members = get_max_members(segment, new_tier)
            if max_members and member_count > max_members:
                logger.warning("Household %d has %d members but tier '%s' allows max %d",
                               user.household_id, member_count, new_tier, max_members)

        logger.info("Household %d changed tier to '%s' (segment: %s)",
                     user.household_id, new_tier, segment)

        await ws_manager.broadcast({"type": "settings_updated"}, household_id=user.household_id)
        return {"ok": True, "tier": new_tier, "segment": segment}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_household_tier: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_household_tier: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/household/pi-setup-script")
async def get_pi_setup_script(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Generate a personalized Pi kiosk setup script with the household's kiosk URL baked in."""
    from fastapi.responses import PlainTextResponse

    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can generate setup scripts")

        # Find or create a kiosk token for this household
        with get_db() as conn:
            row = conn.execute(
                "SELECT token FROM kiosk_tokens WHERE household_id = ? LIMIT 1",
                (user.household_id,),
            ).fetchone()
            if row:
                token = row["token"]
            else:
                token = secrets.token_urlsafe(48)
                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO kiosk_tokens (household_id, token, label, created_at) VALUES (?, ?, ?, ?)",
                    (user.household_id, token, "Pi Kiosk (auto)", now),
                )
                conn.commit()

        # Build kiosk URL from request origin
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost:8001"))
        base_url = f"{scheme}://{host}"
        kiosk_url = f"{base_url}/kiosk?token={token}"

        # Read template and replace placeholder
        template_path = Path(__file__).parent / "pi-kiosk-setup.sh"
        script = template_path.read_text()
        script = script.replace(
            'KIOSK_URL="https://huddle.rodgersgroup.au/kiosk"',
            f'KIOSK_URL="{kiosk_url}"',
        )

        return PlainTextResponse(
            content=script,
            media_type="text/x-shellscript",
            headers={"Content-Disposition": "attachment; filename=huddle-pi-setup.sh"},
        )
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_pi_setup_script: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_pi_setup_script: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


def get_avatar_url(user_id: int) -> str:
    """Return the avatar URL for a user, or empty string if none."""
    path = AVATAR_DIR / f"{user_id}.jpg"
    if path.exists():
        return f"/static/avatars/{user_id}.jpg"
    return ""


@router.post("/api/auth/avatar")
async def upload_avatar(
    request: Request,
    user: TenantContext = Depends(get_current_user),
):
    """Upload a profile image. Accepts multipart form with 'file' field."""
    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="No file uploaded")

        content = await file.read()
        if len(content) > AVATAR_MAX_SIZE:
            raise HTTPException(status_code=400, detail="Image too large (max 5MB)")

        try:
            from PIL import Image, ImageOps
            import io
            img = Image.open(io.BytesIO(content))
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")

            # Crop to square (center crop)
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))

            # Resize to 256x256
            img = img.resize((256, 256), Image.LANCZOS)

            # Save as JPEG
            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            out_path = AVATAR_DIR / f"{user.user_id}.jpg"
            img.save(out_path, "JPEG", quality=85)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

        return {"ok": True, "avatar_url": f"/static/avatars/{user.user_id}.jpg"}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in upload_avatar: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/auth/avatar")
async def delete_avatar(user: TenantContext = Depends(get_current_user)):
    """Delete the current user's profile image."""
    try:
        path = AVATAR_DIR / f"{user.user_id}.jpg"
        if path.exists():
            path.unlink()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in delete_avatar: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# --- PIN-based auth for dependents ---

PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 900  # 15 minutes


def _hash_pin(pin: str) -> str:
    """Hash a PIN with SHA-256. PINs are short, so rate limiting is the real security."""
    return hashlib.sha256(pin.encode()).hexdigest()


@router.post("/api/auth/pin-login")
async def pin_login(request: Request):
    """Authenticate a dependent using household invite code + display name + PIN."""
    try:
        client_ip = request.client.host if request.client else "unknown"
        if rate_limiter.is_limited(client_ip, max_requests=RATE_LIMIT_AUTH * 2, window_seconds=600):
            return JSONResponse(
                status_code=429,
                content={"error": "Too many attempts. Please wait a few minutes."},
            )

        body = await request.json()
        invite_code = body.get("invite_code", "").strip().upper()
        display_name = body.get("display_name", "").strip()
        pin = body.get("pin", "").strip()

        if not invite_code or not display_name or not pin:
            raise HTTPException(status_code=400, detail="Invite code, name, and PIN are required")

        if not pin.isdigit() or len(pin) < 4 or len(pin) > 6:
            raise HTTPException(status_code=400, detail="PIN must be 4-6 digits")

        with get_db() as conn:
            # Find household by invite code
            household = conn.execute(
                "SELECT id, name FROM households WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found. Check your code.")

            household_id = household["id"]

            # Find the member in that household
            member = conn.execute(
                """SELECT hm.user_id, hm.display_name, hm.role, u.pin_hash,
                          u.pin_failed_attempts, u.pin_locked_until
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ? AND LOWER(hm.display_name) = LOWER(?)""",
                (household_id, display_name),
            ).fetchone()

            if not member:
                raise HTTPException(status_code=404, detail="Member not found in this household")

            if not member["pin_hash"]:
                raise HTTPException(status_code=400, detail="No PIN set for this account. Ask a manager to set one.")

            # Check lockout
            if member["pin_locked_until"]:
                locked_until = datetime.fromisoformat(member["pin_locked_until"])
                if datetime.now() < locked_until:
                    remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
                    raise HTTPException(
                        status_code=429,
                        detail=f"Account locked. Try again in {remaining} minute{'s' if remaining != 1 else ''}."
                    )
                else:
                    # Lockout expired, reset
                    conn.execute(
                        "UPDATE users SET pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
                        (member["user_id"],),
                    )

            # Verify PIN
            if _hash_pin(pin) != member["pin_hash"]:
                attempts = (member["pin_failed_attempts"] or 0) + 1
                if attempts >= PIN_MAX_ATTEMPTS:
                    locked_until = (datetime.now() + timedelta(seconds=PIN_LOCKOUT_SECONDS)).isoformat()
                    conn.execute(
                        "UPDATE users SET pin_failed_attempts = ?, pin_locked_until = ? WHERE id = ?",
                        (attempts, locked_until, member["user_id"]),
                    )
                    conn.commit()
                    raise HTTPException(status_code=429, detail="Too many failed attempts. Account locked for 15 minutes.")
                else:
                    conn.execute(
                        "UPDATE users SET pin_failed_attempts = ? WHERE id = ?",
                        (attempts, member["user_id"]),
                    )
                    conn.commit()
                    remaining = PIN_MAX_ATTEMPTS - attempts
                    raise HTTPException(
                        status_code=401,
                        detail=f"Wrong PIN. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
                    )

            # Success — reset failed attempts and issue session
            conn.execute(
                "UPDATE users SET pin_failed_attempts = 0, pin_locked_until = NULL, last_login_at = ? WHERE id = ?",
                (datetime.now().isoformat(), member["user_id"]),
            )
            conn.commit()

        session_token = create_session_token(member["user_id"], household_id)
        response = JSONResponse(content={"ok": True, "message": "Logged in"})
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
        )
        return response

    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in pin_login: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in pin_login: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/auth/pin-members")
async def get_pin_members(request: Request):
    """Get list of members with PINs for a household (by invite code). Used on login screen."""
    try:
        invite_code = request.query_params.get("code", "").strip().upper()
        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required")

        with get_db() as conn:
            household = conn.execute(
                "SELECT id, name FROM households WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found")

            members = conn.execute(
                """SELECT hm.display_name, hm.color, hm.role,
                          CASE WHEN u.pin_hash IS NOT NULL THEN 1 ELSE 0 END as has_pin
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ? AND u.pin_hash IS NOT NULL
                   ORDER BY hm.display_name""",
                (household["id"],),
            ).fetchall()

        return {
            "household_name": household["name"],
            "members": [
                {
                    "display_name": m["display_name"],
                    "color": m["color"],
                    "role": m["role"],
                }
                for m in members
            ],
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_pin_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_pin_members: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/auth/set-pin")
async def set_pin(request: Request, user: TenantContext = Depends(get_current_user)):
    """Set or update PIN for a household member. Manager-only."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can set PINs")

        body = await request.json()
        target_name = body.get("display_name", "").strip()
        pin = body.get("pin", "").strip()

        if not target_name:
            raise HTTPException(status_code=400, detail="display_name is required")

        if not pin:
            # Clear PIN
            with get_db() as conn:
                member = conn.execute(
                    """SELECT hm.user_id FROM household_members hm
                       WHERE hm.household_id = ? AND LOWER(hm.display_name) = LOWER(?)""",
                    (user.household_id, target_name),
                ).fetchone()
                if not member:
                    raise HTTPException(status_code=404, detail="Member not found")
                conn.execute(
                    "UPDATE users SET pin_hash = NULL, pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
                    (member["user_id"],),
                )
                conn.commit()
            return {"ok": True, "message": "PIN removed"}

        if not pin.isdigit() or len(pin) < 4 or len(pin) > 6:
            raise HTTPException(status_code=400, detail="PIN must be 4-6 digits")

        with get_db() as conn:
            member = conn.execute(
                """SELECT hm.user_id FROM household_members hm
                   WHERE hm.household_id = ? AND LOWER(hm.display_name) = LOWER(?)""",
                (user.household_id, target_name),
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")

            conn.execute(
                "UPDATE users SET pin_hash = ?, pin_failed_attempts = 0, pin_locked_until = NULL WHERE id = ?",
                (_hash_pin(pin), member["user_id"]),
            )
            conn.commit()

        return {"ok": True, "message": f"PIN set for {target_name}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in set_pin: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in set_pin: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/auth/pin-status")
async def pin_status(user: TenantContext = Depends(get_current_user)):
    """Get PIN status for all household members. Manager-only."""
    try:
        if user.role != "manager":
            raise HTTPException(status_code=403, detail="Only managers can view PIN status")

        with get_db() as conn:
            members = conn.execute(
                """SELECT hm.display_name, hm.role, hm.color,
                          CASE WHEN u.pin_hash IS NOT NULL THEN 1 ELSE 0 END as has_pin
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ?
                   ORDER BY hm.display_name""",
                (user.household_id,),
            ).fetchall()

        return {
            "members": [
                {
                    "display_name": m["display_name"],
                    "role": m["role"],
                    "color": m["color"],
                    "has_pin": bool(m["has_pin"]),
                }
                for m in members
            ],
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in pin_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in pin_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Manager-Approval Access Flow ───────────────────────────────────────────


@router.get("/api/auth/household-members")
async def get_household_members_by_code(request: Request):
    """Get list of all members for a household by invite code. Used on login screen."""
    try:
        invite_code = request.query_params.get("code", "").strip().upper()
        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required")

        with get_db() as conn:
            household = conn.execute(
                "SELECT id, name FROM households WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found. Check your code and try again.")

            members = conn.execute(
                """SELECT hm.id, hm.display_name, hm.color, hm.role
                   FROM household_members hm
                   WHERE hm.household_id = ?
                   ORDER BY hm.display_name""",
                (household["id"],),
            ).fetchall()

        return {
            "household_name": household["name"],
            "members": [
                {
                    "member_id": m["id"],
                    "display_name": m["display_name"],
                    "color": m["color"],
                    "role": m["role"],
                }
                for m in members
            ],
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_household_members_by_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_household_members_by_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/auth/request-access")
async def request_access(request: Request):
    """Request access to a household. Sends approval email to managers."""
    try:
        data = await request.json()
        invite_code = (data.get("invite_code") or "").strip().upper()
        display_name = (data.get("display_name") or "").strip()
        member_id = data.get("member_id")

        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required")
        if not display_name:
            raise HTTPException(status_code=400, detail="Name is required")

        with get_db() as conn:
            household = conn.execute(
                "SELECT id, name FROM households WHERE invite_code = ?",
                (invite_code,),
            ).fetchone()
            if not household:
                raise HTTPException(status_code=404, detail="Household not found")

            household_id = household["id"]

            # If member_id provided, verify it belongs to this household
            if member_id:
                member = conn.execute(
                    "SELECT id, display_name FROM household_members WHERE id = ? AND household_id = ?",
                    (member_id, household_id),
                ).fetchone()
                if not member:
                    member_id = None  # Fall back to custom name

            # Rate limit: max 3 pending requests per household per 10 minutes
            recent = conn.execute(
                """SELECT COUNT(*) as cnt FROM access_requests
                   WHERE household_id = ? AND status = 'pending'
                   AND created_at > datetime('now', '-10 minutes')""",
                (household_id,),
            ).fetchone()
            if recent and recent["cnt"] >= 3:
                raise HTTPException(status_code=429, detail="Too many requests. Please wait a few minutes.")

            # Create access request token
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now() + timedelta(minutes=30)).isoformat()

            conn.execute(
                """INSERT INTO access_requests (household_id, member_id, token, status, created_at, expires_at)
                   VALUES (?, ?, ?, 'pending', datetime('now'), ?)""",
                (household_id, member_id or 0, token, expires_at),
            )
            conn.commit()

            # Find manager emails for this household
            managers = conn.execute(
                """SELECT u.email, hm.display_name
                   FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE hm.household_id = ? AND hm.role = 'manager'
                   AND u.email NOT LIKE '%@placeholder'""",
                (household_id,),
            ).fetchall()

        # Send approval email to managers
        base_url = str(request.base_url).rstrip("/")
        # Use the external URL if available
        if "huddle.rodgersgroup.au" in request.headers.get("host", ""):
            base_url = "https://huddle.rodgersgroup.au"

        _send_access_approval_email(
            managers=[(m["email"], m["display_name"]) for m in managers],
            requester_name=display_name,
            household_name=household["name"],
            token=token,
            base_url=base_url,
        )

        return {"token": token, "message": "Access requested. Waiting for manager approval."}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in request_access: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in request_access: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/auth/access-status")
async def access_status(request: Request):
    """Poll for access request status. Returns pending/approved/denied/expired. Sets session cookie on approved."""
    try:
        token = request.query_params.get("token", "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="Token is required")

        with get_db() as conn:
            req = conn.execute(
                "SELECT status, expires_at, approved_by FROM access_requests WHERE token = ?",
                (token,),
            ).fetchone()

        if not req:
            return {"status": "expired"}

        if req["status"] == "pending" and datetime.fromisoformat(req["expires_at"]) < datetime.now():
            return {"status": "expired"}

        if req["status"] == "approved" and req["approved_by"]:
            # Set the session cookie so the member's device is logged in
            response = JSONResponse({"status": "approved"})
            response.set_cookie(
                key=COOKIE_NAME,
                value=req["approved_by"],  # This is the session token stored during approval
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=COOKIE_SECURE,
            )
            return response

        return {"status": req["status"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in access_status: %s", e, exc_info=True)
        return {"status": "error"}


@router.get("/auth/approve-access")
async def approve_access(request: Request, token: str):
    """Manager clicks this link from email to approve a member's access."""
    try:
        with get_db() as conn:
            req = conn.execute(
                """SELECT ar.id, ar.household_id, ar.member_id, ar.status, ar.expires_at,
                          hm.user_id, hm.display_name
                   FROM access_requests ar
                   LEFT JOIN household_members hm ON hm.id = ar.member_id AND hm.household_id = ar.household_id
                   WHERE ar.token = ?""",
                (token,),
            ).fetchone()

            if not req:
                return HTMLResponse(_approval_result_page("Request Not Found", "This access request doesn't exist or has already been processed.", False))

            if req["status"] != "pending":
                return HTMLResponse(_approval_result_page("Already Processed", f"This request was already {req['status']}.", req["status"] == "approved"))

            if datetime.fromisoformat(req["expires_at"]) < datetime.now():
                return HTMLResponse(_approval_result_page("Request Expired", "This access request has expired. The member can request again.", False))

            user_id = req["user_id"]
            household_id = req["household_id"]
            display_name = req["display_name"] or "New Member"

            if not user_id:
                # Custom name request (not an existing member) — create placeholder user and member
                placeholder_email = f"{display_name.lower().replace(' ', '_')}_{secrets.token_hex(4)}@placeholder"
                user_id = get_or_create_user(placeholder_email, display_name)
                conn.execute(
                    """INSERT OR IGNORE INTO household_members (household_id, user_id, display_name, color, role, joined_at)
                       VALUES (?, ?, ?, '#4ecdc4', 'member', datetime('now'))""",
                    (household_id, user_id, display_name),
                )
                # Update the member_id on the access request
                new_member = conn.execute(
                    "SELECT id FROM household_members WHERE household_id = ? AND user_id = ?",
                    (household_id, user_id),
                ).fetchone()
                if new_member:
                    conn.execute(
                        "UPDATE access_requests SET member_id = ? WHERE id = ?",
                        (new_member["id"], req["id"]),
                    )

            # Create session for the member
            session_token = create_session_token(user_id, household_id)

            # Mark request as approved
            conn.execute(
                "UPDATE access_requests SET status = 'approved' WHERE id = ?",
                (req["id"],),
            )
            conn.commit()

        # Store session token so polling endpoint can set it
        # We'll use a separate mechanism: store session in the access_requests table
        with get_db() as conn:
            conn.execute(
                "UPDATE access_requests SET approved_by = ? WHERE token = ?",
                (session_token, token),
            )
            conn.commit()

        return HTMLResponse(_approval_result_page("Access Approved", f"{display_name} can now use Huddle on their device. Their screen will load automatically.", True))
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in approve_access: %s", e, exc_info=True)
        return HTMLResponse(_approval_result_page("Error", "Something went wrong. Please try again.", False))


@router.get("/auth/deny-access")
async def deny_access(request: Request, token: str):
    """Manager clicks this link from email to deny a member's access."""
    try:
        with get_db() as conn:
            req = conn.execute(
                "SELECT id, status FROM access_requests WHERE token = ?",
                (token,),
            ).fetchone()

            if not req:
                return HTMLResponse(_approval_result_page("Request Not Found", "This access request doesn't exist.", False))

            if req["status"] != "pending":
                return HTMLResponse(_approval_result_page("Already Processed", f"This request was already {req['status']}.", False))

            conn.execute(
                "UPDATE access_requests SET status = 'denied' WHERE id = ?",
                (req["id"],),
            )
            conn.commit()

        return HTMLResponse(_approval_result_page("Access Denied", "The request has been denied. The member will be notified.", False))
    except Exception as e:
        logger.critical("Unexpected error in deny_access: %s", e, exc_info=True)
        return HTMLResponse(_approval_result_page("Error", "Something went wrong.", False))


def _approval_result_page(title: str, message: str, success: bool) -> str:
    """Generate a simple branded HTML page for approval result."""
    color = "#4ecdc4" if success else "#ff6b6b"
    icon = "&#10004;" if success else "&#10006;"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - Huddle</title>
<style>
body {{ margin:0;padding:40px 20px;background:#1a1d27;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh; }}
.card {{ max-width:400px;text-align:center;background:#242830;border-radius:20px;padding:48px 32px;border:1px solid #333; }}
.icon {{ width:64px;height:64px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:28px;margin-bottom:20px;background:{color}22;color:{color}; }}
h1 {{ font-size:22px;margin:0 0 12px;font-weight:700; }}
p {{ color:#888;font-size:15px;line-height:1.5;margin:0; }}
.logo {{ margin-top:32px;color:#555;font-size:13px; }}
.logo span {{ color:#4ecdc4; }}
</style></head><body>
<div class="card">
<div class="icon">{icon}</div>
<h1>{title}</h1>
<p>{message}</p>
<div class="logo"><span>H</span>uddle</div>
</div></body></html>"""


def _send_access_approval_email(managers: list, requester_name: str, household_name: str, token: str, base_url: str):
    """Send approval email to household managers when someone requests access."""
    try:
        import resend
        from config import RESEND_API_KEY

        if not RESEND_API_KEY:
            logger.info("ACCESS REQUEST for %s to %s: no RESEND_API_KEY, skipping email", requester_name, household_name)
            return

        resend.api_key = RESEND_API_KEY

        approve_url = f"{base_url}/auth/approve-access?token={token}"
        deny_url = f"{base_url}/auth/deny-access?token={token}"

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 20px;">
<tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:440px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
  <tr><td style="background:#1a1d27;padding:32px 24px;text-align:center;">
    <div style="font-size:32px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">
      <span style="color:#4ecdc4;">H</span>uddle
    </div>
    <div style="color:#888;font-size:14px;margin-top:4px;">Access Request</div>
  </td></tr>
  <tr><td style="padding:32px 28px;">
    <h1 style="margin:0 0 8px;font-size:20px;font-weight:700;color:#1a1d27;">Someone wants to join!</h1>
    <p style="margin:0 0 24px;color:#555;font-size:15px;line-height:1.5;">
      <strong>{requester_name}</strong> is requesting access to <strong>{household_name}</strong> on Huddle.
      Their device is waiting for your approval.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td align="center" style="padding:0 8px 0 0;width:50%;">
          <a href="{approve_url}"
             style="display:block;background:#4ecdc4;color:#1a1d27;padding:14px 24px;border-radius:12px;font-size:16px;font-weight:700;text-decoration:none;text-align:center;">
            Approve
          </a>
        </td>
        <td align="center" style="padding:0 0 0 8px;width:50%;">
          <a href="{deny_url}"
             style="display:block;background:#f4f4f5;color:#666;padding:14px 24px;border-radius:12px;font-size:16px;font-weight:600;text-decoration:none;border:1px solid #ddd;text-align:center;">
            Deny
          </a>
        </td>
      </tr>
    </table>
    <p style="margin:24px 0 0;color:#999;font-size:13px;line-height:1.5;">
      This request expires in <strong>30 minutes</strong>. If you don't recognise this person, just ignore this email.
    </p>
  </td></tr>
  <tr><td style="border-top:1px solid #eee;padding:20px 28px;text-align:center;">
    <p style="margin:0;color:#bbb;font-size:12px;">
      Huddle &mdash; huddle.rodgersgroup.au
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

        for manager_email, manager_name in managers:
            resend.Emails.send({
                "from": "Huddle <noreply@huddle.rodgersgroup.au>",
                "to": [manager_email],
                "subject": f"{requester_name} wants to join {household_name} on Huddle",
                "html": html,
            })
            logger.info("Access approval email sent to %s for %s requesting %s", manager_email, requester_name, household_name)
    except Exception as e:
        logger.warning("Failed to send access approval email: %s", e)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.post("/api/admin/force-reauth")
async def admin_force_reauth(request: Request, user: TenantContext = Depends(get_current_user)):
    """Force all users to re-authenticate by bumping auth version and revoking all DB sessions."""
    from config import SUPERADMIN_EMAILS
    with get_db() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user.user_id,)).fetchone()
    if not row or row["email"] not in SUPERADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Superadmin access required")
    count = force_reauth_all()
    logger.info("Force re-auth triggered by user %s — %d sessions revoked, auth version now %d", user.user_id, count, get_auth_version())
    return {"ok": True, "sessions_revoked": count, "auth_version": get_auth_version()}
