"""Add shopping_items table for shared shopping lists."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shopping_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity TEXT,
            category TEXT DEFAULT 'other',
            notes TEXT,
            purchased INTEGER DEFAULT 0,
            added_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            purchased_at TIMESTAMP,
            household_id INTEGER REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_shopping_items_household_id ON shopping_items(household_id);
        CREATE INDEX IF NOT EXISTS idx_shopping_items_purchased ON shopping_items(purchased);
    """)
    conn.commit()

    # Seed the shopping module for all existing households
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing = conn.execute(
                "SELECT id FROM household_modules WHERE household_id = ? AND module_key = 'shopping'",
                (hid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, 'shopping', 1)",
                    (hid,)
                )
        conn.commit()
    except Exception as e:
        logger.warning("Migration 003: seed shopping module: %s", e)
