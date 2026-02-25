"""Add feedback and feedback_votes tables for beta tester feedback."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_type TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL,
            description TEXT,
            screenshot_path TEXT,
            page_context TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            admin_notes TEXT,
            submitted_by TEXT NOT NULL,
            user_agent TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE TABLE IF NOT EXISTS feedback_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL,
            voter TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            UNIQUE(feedback_id, voter, household_id),
            FOREIGN KEY (feedback_id) REFERENCES feedback(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_feedback_household ON feedback(household_id, status);
        CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(household_id, feedback_type);
    """)
    conn.commit()

    # Register feedback module for all existing households (enabled by default for beta)
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing = conn.execute(
                "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = 'feedback'",
                (hid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, 'feedback', 1)",
                    (hid,)
                )
        conn.commit()
    except Exception as e:
        logger.warning("Migration 009: seed feedback module: %s", e)
