"""Client error logging endpoint.

Receives batched error reports from the frontend and logs them
to a dedicated rotating log file for debugging and monitoring.
"""

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request

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
        body = await request.json()
        errors = body.get("errors", [])
        if not isinstance(errors, list) or len(errors) == 0:
            return {"ok": True, "logged": 0}

        # Cap at 20 errors per request to prevent abuse
        for err in errors[:20]:
            entry = {
                "ts": err.get("timestamp", datetime.utcnow().isoformat()),
                "user": err.get("user", "unknown"),
                "url": err.get("url", ""),
                "message": err.get("message", "")[:500],
                "source": err.get("source", "")[:200],
                "lineno": err.get("lineno", 0),
                "colno": err.get("colno", 0),
                "stack": err.get("stack", "")[:1000],
                "ua": err.get("userAgent", "")[:200],
                "ip": request.client.host if request.client else "",
            }
            _client_logger.info(json.dumps(entry))

        return {"ok": True, "logged": min(len(errors), 20)}
    except Exception:
        return {"ok": False, "logged": 0}
