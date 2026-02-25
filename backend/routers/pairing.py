"""Kiosk pairing API — QR code-based display pairing."""

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user, TenantContext, RateLimiter
from db import get_db

logger = logging.getLogger("huddle")

router = APIRouter()

# Uppercase alphanumeric minus confusable characters (0/O, 1/I/L)
PAIRING_CHARSET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CODE_TTL_MINUTES = 10

_pairing_rate_limiter = RateLimiter()


@router.post("/api/kiosk/pairing-code")
def create_pairing_code(request: Request):
    """Generate a 6-char pairing code for kiosk display. No auth required."""
    try:
        client_ip = request.client.host if request.client else "unknown"

        if _pairing_rate_limiter.is_limited(client_ip, max_requests=200, window_seconds=3600):
            raise HTTPException(status_code=429, detail="Too many pairing requests. Please wait.")

        expires_at = (datetime.now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()

        # Retry loop for uniqueness collisions (astronomically unlikely)
        for _ in range(10):
            code = "".join(secrets.choice(PAIRING_CHARSET) for _ in range(CODE_LENGTH))
            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO kiosk_pairing_codes (code, expires_at, request_ip) VALUES (?, ?, ?)",
                        (code, expires_at, client_ip),
                    )
                    conn.commit()
                    # Append Z so JS interprets as UTC (server runs in UTC)
                    return {"code": code, "expires_at": expires_at + "Z"}
            except sqlite3.IntegrityError:
                continue  # code collision, retry

        raise HTTPException(status_code=500, detail="Failed to generate unique code")
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_pairing_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_pairing_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/kiosk/pairing-status")
def check_pairing_status(code: str):
    """Poll endpoint for kiosk. Returns status and token when paired."""
    try:
        code = code.strip().upper()
        if not code or len(code) != CODE_LENGTH:
            raise HTTPException(status_code=400, detail="Invalid code format")

        with get_db() as conn:
            row = conn.execute(
                "SELECT id, household_id, kiosk_token_id, expires_at, claimed_at FROM kiosk_pairing_codes WHERE code = ?",
                (code,),
            ).fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Code not found")

            # Check claimed status BEFORE expiry — once claimed, always return paired
            if row["claimed_at"] and row["kiosk_token_id"]:
                token_row = conn.execute(
                    "SELECT token FROM kiosk_tokens WHERE id = ?",
                    (row["kiosk_token_id"],),
                ).fetchone()
                if token_row:
                    return {"status": "paired", "token": token_row["token"]}

            if datetime.fromisoformat(row["expires_at"]) < datetime.now():
                return {"status": "expired"}

            return {"status": "waiting"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in check_pairing_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in check_pairing_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/kiosk/claim-code")
async def claim_pairing_code(request: Request, user: TenantContext = Depends(get_current_user)):
    """Claim a pairing code for the user's household. Any authenticated member."""
    try:

        body = await request.json()
        code = body.get("code", "").strip().upper()
        label = body.get("label", "Kiosk Display").strip()

        if not code or len(code) != CODE_LENGTH:
            logger.warning("Validation failed in claim_pairing_code: invalid code format")
            raise HTTPException(status_code=400, detail="Invalid pairing code")
        if len(label) > 100:
            logger.warning("Validation failed in claim_pairing_code: label too long")
            raise HTTPException(status_code=400, detail="Label too long (max 100 characters)")

        client_ip = request.client.host if request.client else "unknown"

        with get_db() as conn:
            row = conn.execute(
                "SELECT id, household_id, claimed_at, expires_at FROM kiosk_pairing_codes WHERE code = ?",
                (code,),
            ).fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Pairing code not found")

            if row["claimed_at"]:
                raise HTTPException(status_code=409, detail="Code already claimed")

            if datetime.fromisoformat(row["expires_at"]) < datetime.now():
                raise HTTPException(status_code=410, detail="Pairing code has expired")

            # Create kiosk token (same pattern as auth.py:create_kiosk_token)
            token_value = secrets.token_urlsafe(48)
            now = datetime.now().isoformat()

            cursor = conn.execute(
                "INSERT INTO kiosk_tokens (household_id, token, label, created_at) VALUES (?, ?, ?, ?)",
                (user.household_id, token_value, label, now),
            )
            kiosk_token_id = cursor.lastrowid

            # Claim the pairing code
            conn.execute(
                "UPDATE kiosk_pairing_codes SET household_id = ?, kiosk_token_id = ?, claimed_at = ?, claim_ip = ? WHERE id = ?",
                (user.household_id, kiosk_token_id, now, client_ip, row["id"]),
            )
            conn.commit()

        logger.info("Pairing code %s claimed by user %d for household %d", code, user.user_id, user.household_id)
        return {"ok": True, "message": "Display paired successfully"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in claim_pairing_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in claim_pairing_code: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
