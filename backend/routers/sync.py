"""Bulk sync endpoint for delta synchronization.

Implements AnyList-inspired timestamp-based delta sync: client sends last-known
timestamps per resource, server returns only items changed since those timestamps.
This reduces bandwidth and improves perceived performance for multi-device households.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_user, TenantContext
from db import get_db

logger = logging.getLogger("huddle")

router = APIRouter()


# Mapping of sync resource names to their database tables and key columns.
# Each entry: (table_name, columns_to_select)
_SYNC_RESOURCES = {
    "chores": "chores",
    "meals": "meals",
    "adhoc_tasks": "adhoc_tasks",
    "calendar_events": "calendar_events",
    "bills": "bills",
    "shopping_items": "shopping_items",
    "inventory_items": "inventory_items",
    "recipes": "recipes",
    "noticeboard_posts": "noticeboard_posts",
    "pets": "pets",
    "routines": "routines",
    "house_rules": "house_rules",
    "selfcare_items": "selfcare_items",
    "freezer_items": "freezer_items",
    "polls": "polls",
    "allowances": "allowances",
    "rewards": "rewards",
    "assignments": "assignments",
}


class SyncRequest(BaseModel):
    """Client sends last-known timestamp per resource. null = full fetch."""
    timestamps: dict[str, Optional[str]] = {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return column in [row[1] for row in cursor.fetchall()]


@router.post("/api/sync")
def bulk_sync(body: SyncRequest, tenant: TenantContext = Depends(get_current_user)):
    """Fetch changes across multiple resources in a single request.

    For each resource in the request:
    - If timestamp is null: return all non-deleted items
    - If timestamp is set: return only items with updated_at > timestamp
    - Always include IDs of items deleted since the given timestamp

    Response includes server_time for the client to store and send on the next sync.
    """
    household_id = tenant.household_id
    server_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    changes = {}
    deleted = {}

    try:
        with get_db() as conn:
            for resource, since in body.timestamps.items():
                table = _SYNC_RESOURCES.get(resource)
                if not table or not _table_exists(conn, table):
                    continue

                has_updated = _has_column(conn, table, "updated_at")
                has_deleted = _has_column(conn, table, "deleted_at")
                has_household = _has_column(conn, table, "household_id")

                # Build the query for changed items
                if since and has_updated:
                    if has_household:
                        rows = conn.execute(
                            f"SELECT * FROM {table} WHERE household_id = ? AND updated_at > ? AND (deleted_at IS NULL)",
                            (household_id, since),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"SELECT * FROM {table} WHERE updated_at > ? AND (deleted_at IS NULL)",
                            (since,),
                        ).fetchall()
                else:
                    # Full fetch — no timestamp or table doesn't support delta
                    if has_household:
                        where = "WHERE household_id = ?"
                        params = (household_id,)
                        if has_deleted:
                            where += " AND deleted_at IS NULL"
                        rows = conn.execute(
                            f"SELECT * FROM {table} {where}", params
                        ).fetchall()
                    else:
                        where = ""
                        if has_deleted:
                            where = "WHERE deleted_at IS NULL"
                        rows = conn.execute(
                            f"SELECT * FROM {table} {where}"
                        ).fetchall()

                changes[resource] = [dict(r) for r in rows]

                # Get deleted item IDs since the timestamp
                if since and has_deleted:
                    if has_household:
                        del_rows = conn.execute(
                            f"SELECT id FROM {table} WHERE household_id = ? AND deleted_at > ?",
                            (household_id, since),
                        ).fetchall()
                    else:
                        del_rows = conn.execute(
                            f"SELECT id FROM {table} WHERE deleted_at > ?",
                            (since,),
                        ).fetchall()
                    del_ids = [r["id"] for r in del_rows]
                    if del_ids:
                        deleted[resource] = del_ids

        return {
            "server_time": server_time,
            "changes": changes,
            "deleted": deleted,
        }

    except sqlite3.Error as e:
        logger.error("Database error in bulk_sync: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
