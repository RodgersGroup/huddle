"""Add selfcare module for medication and personal care tracking."""

import logging

logger = logging.getLogger("huddle")


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS selfcare_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'medication',
            assigned_to TEXT NOT NULL,
            frequency_days INTEGER NOT NULL DEFAULT 0,
            reminder_enabled INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            icon TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE TABLE IF NOT EXISTS selfcare_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            logged_by TEXT NOT NULL,
            logged_at TEXT NOT NULL DEFAULT (datetime('now')),
            notes TEXT,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (item_id) REFERENCES selfcare_items(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_selfcare_items_household ON selfcare_items(household_id);
        CREATE INDEX IF NOT EXISTS idx_selfcare_logs_item ON selfcare_logs(item_id, logged_at);
        CREATE INDEX IF NOT EXISTS idx_selfcare_logs_household ON selfcare_logs(household_id);
    """)
    conn.commit()

    # Seed module as disabled for existing households
    for h in conn.execute("SELECT id FROM households").fetchall():
        existing = conn.execute(
            "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = ?",
            (h["id"], "selfcare"),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, 0)",
                (h["id"], "selfcare"),
            )
    conn.commit()
