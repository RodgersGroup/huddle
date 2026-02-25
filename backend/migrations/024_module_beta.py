"""Track module beta status in database instead of hardcoded flags."""

import logging

logger = logging.getLogger("huddle")


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS module_beta (
            module_key TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    # Seed from any hardcoded beta flags in AVAILABLE_MODULES
    # Currently: selfcare
    conn.execute(
        "INSERT OR IGNORE INTO module_beta (module_key) VALUES (?)",
        ("selfcare",),
    )
    conn.commit()
