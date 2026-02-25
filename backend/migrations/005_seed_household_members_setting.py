"""Seed household_members setting from household_members table.

Previously, settings.py had hardcoded defaults for household_members and
member_colors. Those defaults have been removed (replaced with empty []/{}).
This migration ensures every existing household has its member names
explicitly stored in the settings table, derived from the household_members
table rows.
"""

import json
import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]

            # Only seed if the setting doesn't already exist
            existing = conn.execute(
                "SELECT value FROM settings WHERE key = 'household_members' AND household_id = ?",
                (hid,),
            ).fetchone()
            if existing:
                continue

            # Build member list from household_members table
            members = conn.execute(
                "SELECT display_name FROM household_members WHERE household_id = ? ORDER BY joined_at",
                (hid,),
            ).fetchall()
            names = [m[0] if isinstance(m, tuple) else m["display_name"] for m in members]

            if names:
                conn.execute(
                    "INSERT INTO settings (key, value, household_id) VALUES (?, ?, ?)",
                    ("household_members", json.dumps(names), hid),
                )
                logger.info("Migration 005: seeded household_members for household %d: %s", hid, names)

        conn.commit()
    except Exception as e:
        logger.warning("Migration 005: seed household_members setting: %s", e)
