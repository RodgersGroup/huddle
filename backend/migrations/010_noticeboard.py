"""Add noticeboard table for household announcements and check-ins."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS noticeboard_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT,
            post_type TEXT NOT NULL DEFAULT 'notice',
            priority TEXT NOT NULL DEFAULT 'normal',
            posted_by TEXT NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_noticeboard_household ON noticeboard_posts(household_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_noticeboard_pinned ON noticeboard_posts(household_id, pinned);
    """)
    conn.commit()

    # Register noticeboard module for all existing households
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing = conn.execute(
                "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = 'noticeboard'",
                (hid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, 'noticeboard', 1)",
                    (hid,)
                )
        conn.commit()
    except Exception as e:
        logger.warning("Migration 010: seed noticeboard module: %s", e)
