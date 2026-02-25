"""Page view tracking for Huddle.

Extracted from app.py to avoid a circular import: background.py needs
flush_page_views() but importing from app.py pulls in the entire FastAPI
application (which imports background.py during startup).
"""

import logging
import sqlite3
import threading
from datetime import datetime

from db import get_db

logger = logging.getLogger("huddle")

TRACKED_PAGES = {"/", "/mobile", "/onboard", "/kiosk", "/status", "/superadmin", "/pair"}

_page_view_buffer = {}  # {(page, date): count} — flushed periodically
_page_view_lock = threading.Lock()


def record_page_view(path: str):
    """Buffer a single page view for later flushing to the database."""
    if path not in TRACKED_PAGES:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    key = (path, today)
    with _page_view_lock:
        _page_view_buffer[key] = _page_view_buffer.get(key, 0) + 1


async def flush_page_views():
    """Flush buffered page views to DB. Called periodically from background task."""
    with _page_view_lock:
        if not _page_view_buffer:
            return
        buffer_copy = dict(_page_view_buffer)
        _page_view_buffer.clear()
    try:
        with get_db() as conn:
            for (page, view_date), count in buffer_copy.items():
                conn.execute(
                    "INSERT INTO page_views (page, view_date, count) VALUES (?, ?, ?) "
                    "ON CONFLICT(page, view_date) DO UPDATE SET count = count + ?",
                    (page, view_date, count, count),
                )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning("Failed to flush page views (DB): %s", e)
    except Exception as e:
        logger.warning("Failed to flush page views: %s", e)
