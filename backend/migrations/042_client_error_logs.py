"""Create client_error_logs table for manager-visible device error tracking.

Previously, client errors were only written to a rotating log file.
This migration stores them in SQLite so managers can view errors from
household members' devices via the settings UI.
"""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER,
            user_id INTEGER,
            display_name TEXT,
            ts TEXT NOT NULL,
            url TEXT,
            message TEXT,
            source TEXT,
            lineno INTEGER DEFAULT 0,
            colno INTEGER DEFAULT 0,
            stack TEXT,
            user_agent TEXT,
            ip TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_client_error_logs_household_created
        ON client_error_logs (household_id, created_at DESC)
    """)
    conn.commit()
    logger.info("042_client_error_logs: created client_error_logs table")
