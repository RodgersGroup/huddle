"""Add dependent_permissions table for parental controls."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dependent_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            module_key TEXT NOT NULL,
            access_level TEXT NOT NULL DEFAULT 'hidden',
            FOREIGN KEY (household_id) REFERENCES households(id),
            FOREIGN KEY (member_id) REFERENCES household_members(id),
            UNIQUE(household_id, member_id, module_key)
        );

        CREATE INDEX IF NOT EXISTS idx_dep_perms_member ON dependent_permissions(household_id, member_id);
    """)
    conn.commit()
