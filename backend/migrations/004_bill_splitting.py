"""Add bill splitting columns for expense tracking."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bills)").fetchall()]
    if "split_type" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN split_type TEXT DEFAULT 'equal'")
    if "split_between" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN split_between TEXT DEFAULT '[]'")
    conn.commit()
