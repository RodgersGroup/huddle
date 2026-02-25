"""
Add missing start_date column to chores table.

The column was defined in 001_initial_schema.py's CREATE TABLE IF NOT EXISTS,
but because the chores table already existed before the migration system was
introduced, the CREATE was skipped and the column was never added.
"""
import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    cursor = conn.execute("PRAGMA table_info(chores)")
    columns = [row[1] for row in cursor.fetchall()]
    if "start_date" not in columns:
        conn.execute("ALTER TABLE chores ADD COLUMN start_date TEXT")
        conn.commit()
        logger.info("Migrated chores: added start_date column")
    else:
        logger.info("chores.start_date already exists, skipping")
