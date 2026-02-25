"""Add assignments table for homework and assignment tracking."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            subject TEXT,
            assigned_to TEXT NOT NULL,
            due_date TEXT,
            due_time TEXT,
            status TEXT NOT NULL DEFAULT 'todo',
            priority TEXT NOT NULL DEFAULT 'normal',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_assignments_household ON assignments(household_id, status);
        CREATE INDEX IF NOT EXISTS idx_assignments_person ON assignments(household_id, assigned_to);
        CREATE INDEX IF NOT EXISTS idx_assignments_due ON assignments(household_id, due_date);
    """)
    conn.commit()

    # Register module for existing households
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing = conn.execute(
                "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = 'assignments'",
                (hid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, 'assignments', 0)",
                    (hid,)
                )
        conn.commit()
    except Exception as e:
        logger.warning("Migration 013: seed assignments module: %s", e)
