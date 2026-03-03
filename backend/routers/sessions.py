"""Session management endpoints (multi-device support).

Allows users to list their active sessions and selectively revoke them,
similar to AnyList's "Manage Devices" or Gmail's "Where you're signed in".
"""

import json
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from auth import get_current_user, TenantContext, revoke_session, revoke_all_sessions
from db import get_db

logger = logging.getLogger("huddle")
router = APIRouter()


def _get_current_session_id(request: Request) -> str | None:
    """Extract the session_id from the current request. Returns None (cookie auth has no session_id)."""
    return None


@router.get("/api/auth/sessions")
async def list_sessions(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """List all active (non-revoked, non-expired) sessions for the current user."""
    now = datetime.now().isoformat()
    current_session_id = _get_current_session_id(request)

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, device_id, device_name, ip_address, created_at, last_used_at, expires_at
               FROM sessions
               WHERE user_id = ? AND revoked = 0 AND expires_at > ?
               ORDER BY last_used_at DESC""",
            (tenant.user_id, now),
        ).fetchall()

    sessions = []
    for row in rows:
        sessions.append({
            "id": row["id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "ip_address": row["ip_address"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "expires_at": row["expires_at"],
            "is_current": row["id"] == current_session_id,
        })

    return {"sessions": sessions, "count": len(sessions)}


@router.delete("/api/auth/sessions/{session_id}")
async def delete_session(session_id: str, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Revoke a specific session (log out a device)."""
    with get_db() as conn:
        session = conn.execute(
            "SELECT id, user_id FROM sessions WHERE id = ? AND revoked = 0",
            (session_id,),
        ).fetchone()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session["user_id"] != tenant.user_id:
        raise HTTPException(status_code=403, detail="Cannot revoke another user's session")

    revoke_session(session_id)
    logger.info("Session %s revoked by user %s", session_id[:8], tenant.user_id)
    return {"ok": True, "message": "Session revoked"}


@router.delete("/api/auth/sessions")
async def delete_all_sessions(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Revoke all sessions except the current one (logout everywhere else)."""
    current_session_id = _get_current_session_id(request)
    revoke_all_sessions(tenant.user_id, except_session_id=current_session_id)
    logger.info("All sessions revoked for user %s (except %s)", tenant.user_id, current_session_id and current_session_id[:8])
    return {"ok": True, "message": "All other sessions revoked"}
