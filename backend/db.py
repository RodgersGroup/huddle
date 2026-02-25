"""
Database module for Huddle.
Extracted from app.py - provides database connection, schema, and migrations.

Uses thread-local connection caching so each thread reuses the same
SQLite connection instead of opening/closing on every request.
"""

import atexit
import logging
import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger("huddle")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "chores.db"

# Thread-local storage for connection reuse
_local = threading.local()


def _get_thread_connection() -> sqlite3.Connection:
    """Return the cached connection for the current thread, creating one if needed."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -8000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 134217728")
        _local.conn = conn
        logger.debug("Created new DB connection for thread %s", threading.current_thread().name)
    return conn


@contextmanager
def get_db():
    """Database connection context manager with thread-local caching.

    Connections are reused within the same thread. The context manager
    commits on clean exit and rolls back on exception, but does NOT close
    the connection — it stays cached for the next request on this thread.
    """
    conn = _get_thread_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def close_all_connections():
    """Close the connection cached on the current thread (if any).

    Called at interpreter shutdown via atexit. Background threads that
    end naturally will have their thread-local storage garbage-collected.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


atexit.register(close_all_connections)


def init_db():
    """Initialise the database schema via the migrations system."""
    with get_db() as conn:
        # Enable WAL mode for better concurrent read performance
        conn.execute("PRAGMA journal_mode=WAL")

        # Run all pending migrations
        from migrations.runner import run_migrations
        run_migrations(conn)
