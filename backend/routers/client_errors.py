"""Client error logging endpoint.

Receives batched error reports from the frontend and logs them
to a dedicated rotating log file AND SQLite for manager visibility.
"""

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import get_current_user, get_optional_user, TenantContext
from db import get_db

router = APIRouter()

# Dedicated logger for client errors — separate from the main huddle log
_client_logger = logging.getLogger("huddle.client")
if not _client_logger.handlers:
    _log_dir = Path(__file__).parent.parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _handler = logging.handlers.RotatingFileHandler(
        _log_dir / "client_errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _client_logger.addHandler(_handler)
    _client_logger.setLevel(logging.INFO)
    _client_logger.propagate = False  # Don't duplicate into main huddle log


@router.post("/api/client-errors")
async def log_client_errors(request: Request):
    """Receive batched client-side error reports and log them."""
    try:
        # Resolve user optionally — errors can come from unauthenticated pages
        tenant = await get_optional_user(request)

        body = await request.json()
        errors = body.get("errors", [])
        if not isinstance(errors, list) or len(errors) == 0:
            return {"ok": True, "logged": 0}

        ip = request.client.host if request.client else ""

        # Cap at 20 errors per request to prevent abuse
        for err in errors[:20]:
            ts = err.get("timestamp", datetime.utcnow().isoformat())
            display_name = err.get("user", "unknown")
            entry = {
                "ts": ts,
                "user": display_name,
                "url": err.get("url", ""),
                "message": err.get("message", "")[:500],
                "source": err.get("source", "")[:200],
                "lineno": err.get("lineno", 0),
                "colno": err.get("colno", 0),
                "stack": err.get("stack", "")[:1000],
                "ua": err.get("userAgent", "")[:200],
                "ip": ip,
            }
            _client_logger.info(json.dumps(entry))

            # Write to SQLite if we have household context
            if tenant and tenant.household_id:
                try:
                    with get_db() as conn:
                        conn.execute(
                            """INSERT INTO client_error_logs
                               (household_id, user_id, display_name, ts, url, message, source, lineno, colno, stack, user_agent, ip)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                tenant.household_id,
                                tenant.user_id,
                                display_name[:100],
                                ts,
                                entry["url"][:500],
                                entry["message"],
                                entry["source"],
                                entry["lineno"],
                                entry["colno"],
                                entry["stack"],
                                entry["ua"],
                                ip,
                            ),
                        )
                        conn.commit()
                except Exception:
                    pass  # Don't fail the request if DB write fails

        return {"ok": True, "logged": min(len(errors), 20)}
    except Exception:
        return {"ok": False, "logged": 0}


@router.get("/api/client-errors")
async def get_client_errors(
    user: TenantContext = Depends(get_current_user),
    user_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Get recent client errors for the household. Manager-only."""
    if user.role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can view error logs")

    with get_db() as conn:
        if user_id:
            rows = conn.execute(
                """SELECT id, user_id, display_name, ts, url, message, source, lineno, colno, stack, user_agent, ip, created_at
                   FROM client_error_logs
                   WHERE household_id = ? AND user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user.household_id, user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, user_id, display_name, ts, url, message, source, lineno, colno, stack, user_agent, ip, created_at
                   FROM client_error_logs
                   WHERE household_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user.household_id, limit),
            ).fetchall()

    return {
        "errors": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "display_name": r["display_name"],
                "ts": r["ts"],
                "url": r["url"],
                "message": r["message"],
                "source": r["source"],
                "lineno": r["lineno"],
                "colno": r["colno"],
                "stack": r["stack"],
                "user_agent": r["user_agent"],
                "ip": r["ip"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.delete("/api/client-errors")
async def clear_client_errors(user: TenantContext = Depends(get_current_user)):
    """Clear all client error logs for the household. Manager-only."""
    if user.role != "manager":
        raise HTTPException(status_code=403, detail="Only managers can clear error logs")

    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM client_error_logs WHERE household_id = ?",
            (user.household_id,),
        )
        conn.commit()

    return {"ok": True, "deleted": result.rowcount}
