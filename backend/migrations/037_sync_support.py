"""Add version and updated_at columns to core tables for optimistic locking and delta sync.

Adds:
- `version` (INTEGER DEFAULT 1) for optimistic locking (409 Conflict on stale writes)
- `updated_at` (TEXT) for timestamp-based delta sync (client sends last sync time, server returns only changes)
- `deleted_at` (TEXT) for soft deletes (delta sync can report deleted items)

Only adds columns to tables that are actively edited by users and broadcast via WebSocket.
Tables that already have updated_at (e.g., calendar_events, inventory_items) get version + deleted_at only.
"""

import logging
import sqlite3

logger = logging.getLogger("huddle")

# Tables that need all three columns (version, updated_at, deleted_at)
_TABLES_FULL = [
    "chores",
    "meals",
    "adhoc_tasks",
    "bills",
    "polls",
    "shopping_items",
    "recipes",
    "noticeboard_posts",
    "pets",
    "routines",
    "house_rules",
    "selfcare_items",
    "freezer_items",
    "allowances",
    "rewards",
    "assignments",
]

# Tables that already have updated_at — only add version + deleted_at
_TABLES_PARTIAL = [
    "calendar_events",
    "inventory_items",
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return column in [row[1] for row in cursor.fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def up(conn: sqlite3.Connection):
    for table in _TABLES_FULL + _TABLES_PARTIAL:
        if not _table_exists(conn, table):
            logger.info("037_sync_support: table %s does not exist, skipping", table)
            continue

        # Add version column
        if not _has_column(conn, table, "version"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN version INTEGER DEFAULT 1")
            logger.info("037_sync_support: added version to %s", table)

        # Add updated_at column (skip tables that already have it)
        if table not in _TABLES_PARTIAL and not _has_column(conn, table, "updated_at"):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN updated_at TEXT DEFAULT NULL"
            )
            # Backfill existing rows with created_at or current time
            try:
                conn.execute(
                    f"UPDATE {table} SET updated_at = COALESCE(created_at, datetime('now')) WHERE updated_at IS NULL"
                )
            except Exception:
                conn.execute(
                    f"UPDATE {table} SET updated_at = datetime('now') WHERE updated_at IS NULL"
                )
            logger.info("037_sync_support: added updated_at to %s", table)

        # Add deleted_at column for soft deletes
        if not _has_column(conn, table, "deleted_at"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT DEFAULT NULL")
            logger.info("037_sync_support: added deleted_at to %s", table)

    # Create indexes for delta sync queries (WHERE updated_at > ? AND household_id = ?)
    for table in _TABLES_FULL + _TABLES_PARTIAL:
        if not _table_exists(conn, table):
            continue
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_updated_at ON {table}(updated_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_deleted_at ON {table}(deleted_at)"
            )
        except Exception as e:
            logger.warning("037_sync_support: index creation on %s: %s", table, e)

    conn.commit()
