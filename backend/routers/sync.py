"""Bulk sync endpoint for delta synchronization.

Implements AnyList-inspired timestamp-based delta sync: client sends last-known
timestamps per resource, server returns only items changed since those timestamps.
This reduces bandwidth and improves perceived performance for multi-device households.

Also provides POST /api/sync/push for replaying offline-queued mutations as a batch.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_user, TenantContext
from db import get_db
from websocket import manager

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


# ---------------------------------------------------------------------------
# Offline queue replay — POST /api/sync/push
# ---------------------------------------------------------------------------

class _PushOp(BaseModel):
    id: int
    url: str
    method: str
    body: Optional[str] = None
    tempId: Optional[str] = None
    timestamp: Optional[str] = None


class PushRequest(BaseModel):
    operations: list[_PushOp]


# URL patterns matched to mutation handlers.
# Each returns (status, extra_dict) operating directly on DB.
_URL_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, http_method, handler_key)
    (r"^/api/adhoc$", "POST", "adhoc_create"),
    (r"^/api/adhoc/(\d+)/complete$", "POST", "adhoc_complete"),
    (r"^/api/adhoc/(\d+)$", "DELETE", "adhoc_delete"),
    (r"^/api/chores/(\d+)/complete$", "POST", "chore_complete"),
    (r"^/api/chores/(\d+)/skip$", "POST", "chore_skip"),
    (r"^/api/shopping$", "POST", "shopping_create"),
    (r"^/api/shopping/(\d+)$", "DELETE", "shopping_delete"),
    (r"^/api/shopping/(\d+)/purchase$", "POST", "shopping_purchase"),
    (r"^/api/shopping/(\d+)/unpurchase$", "POST", "shopping_unpurchase"),
]


def _match_op(url: str, method: str):
    """Match a queued URL+method to a handler key, returning (key, regex_match)."""
    for pattern, expected_method, key in _URL_PATTERNS:
        if method.upper() == expected_method:
            m = re.match(pattern, url)
            if m:
                return key, m
    return None, None


def _parse_body(raw: Optional[str]) -> dict:
    """Parse JSON body string, returning empty dict on failure."""
    if not raw:
        return {}
    try:
        import json
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _exec_op(conn: sqlite3.Connection, key: str, match, body: dict,
             household_id: int, person: str) -> tuple[str, dict]:
    """Execute a single offline mutation. Returns (status, extras)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if key == "adhoc_create":
        name = body.get("name", "").strip()
        if not name:
            return "error", {"error": "Missing task name"}
        cur = conn.execute(
            "INSERT INTO adhoc_tasks (name, description, added_by, task_type, assigned_to, household_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, body.get("description", ""), person,
             body.get("task_type", "general"), body.get("assigned_to", ""), household_id),
        )
        return "ok", {"serverId": cur.lastrowid}

    if key == "adhoc_complete":
        task_id = int(match.group(1))
        row = conn.execute(
            "SELECT id FROM adhoc_tasks WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
            (task_id, household_id),
        ).fetchone()
        if not row:
            return "conflict", {"error": "Task not found or deleted"}
        conn.execute(
            "UPDATE adhoc_tasks SET completed = 1, completed_by = ?, completed_at = ?"
            " WHERE id = ? AND household_id = ?",
            (person, now, task_id, household_id),
        )
        return "ok", {"serverId": task_id}

    if key == "adhoc_delete":
        task_id = int(match.group(1))
        conn.execute(
            "UPDATE adhoc_tasks SET deleted_at = datetime('now'), updated_at = datetime('now')"
            " WHERE id = ? AND household_id = ?",
            (task_id, household_id),
        )
        return "ok", {"serverId": task_id}

    if key == "chore_complete":
        chore_id = int(match.group(1))
        chore = conn.execute(
            "SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
            (chore_id, household_id),
        ).fetchone()
        if not chore:
            return "conflict", {"error": "Chore not found or deleted"}
        completed_by = body.get("completed_by") or person
        completed_with = body.get("completed_with", "")
        conn.execute(
            "INSERT INTO completions (chore_id, completed_by, completed_with, completed_at, household_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (chore_id, completed_by, completed_with, now, household_id),
        )
        # Advance rotation if solo
        if not completed_with:
            people = [p.strip() for p in (chore["people"] or "").split(",") if p.strip()]
            if people:
                idx = chore["current_person_index"] or 0
                new_idx = (idx + 1) % len(people)
                conn.execute(
                    "UPDATE chores SET current_person_index = ? WHERE id = ? AND household_id = ?",
                    (new_idx, chore_id, household_id),
                )
        return "ok", {"serverId": chore_id}

    if key == "chore_skip":
        chore_id = int(match.group(1))
        chore = conn.execute(
            "SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
            (chore_id, household_id),
        ).fetchone()
        if not chore:
            return "conflict", {"error": "Chore not found or deleted"}
        skipped_by = body.get("skipped_by") or person
        conn.execute(
            "INSERT INTO completions (chore_id, completed_by, completed_at, household_id)"
            " VALUES (?, ?, ?, ?)",
            (chore_id, f"SKIPPED:{skipped_by}", now, household_id),
        )
        people = [p.strip() for p in (chore["people"] or "").split(",") if p.strip()]
        if people:
            idx = chore["current_person_index"] or 0
            new_idx = (idx + 1) % len(people)
            conn.execute(
                "UPDATE chores SET current_person_index = ? WHERE id = ? AND household_id = ?",
                (new_idx, chore_id, household_id),
            )
        return "ok", {"serverId": chore_id}

    if key == "shopping_create":
        name = body.get("name", "").strip()
        if not name:
            return "error", {"error": "Missing item name"}
        # Duplicate detection
        existing = conn.execute(
            "SELECT id, quantity FROM shopping_items"
            " WHERE LOWER(name) = LOWER(?) AND purchased = 0 AND household_id = ? AND deleted_at IS NULL",
            (name, household_id),
        ).fetchone()
        if existing:
            new_qty = body.get("quantity", "1")
            try:
                merged = str(int(existing["quantity"] or "1") + int(new_qty))
            except (ValueError, TypeError):
                merged = f"{existing['quantity']}, {new_qty}"
            conn.execute(
                "UPDATE shopping_items SET quantity = ? WHERE id = ? AND household_id = ?",
                (merged, existing["id"], household_id),
            )
            return "ok", {"serverId": existing["id"]}
        cur = conn.execute(
            "INSERT INTO shopping_items (name, quantity, category, notes, added_by, shop, household_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, body.get("quantity", "1"), body.get("category", ""),
             body.get("notes", ""), person, body.get("shop", ""), household_id),
        )
        return "ok", {"serverId": cur.lastrowid}

    if key == "shopping_delete":
        item_id = int(match.group(1))
        conn.execute(
            "UPDATE shopping_items SET deleted_at = datetime('now'), updated_at = datetime('now')"
            " WHERE id = ? AND household_id = ?",
            (item_id, household_id),
        )
        return "ok", {"serverId": item_id}

    if key == "shopping_purchase":
        item_id = int(match.group(1))
        conn.execute(
            "UPDATE shopping_items SET purchased = 1, purchased_at = ?"
            " WHERE id = ? AND household_id = ?",
            (now, item_id, household_id),
        )
        return "ok", {"serverId": item_id}

    if key == "shopping_unpurchase":
        item_id = int(match.group(1))
        conn.execute(
            "UPDATE shopping_items SET purchased = 0, purchased_at = NULL"
            " WHERE id = ? AND household_id = ?",
            (item_id, household_id),
        )
        return "ok", {"serverId": item_id}

    return "error", {"error": f"Unknown operation: {key}"}


@router.post("/api/sync/push")
async def sync_push(body: PushRequest, tenant: TenantContext = Depends(get_current_user)):
    """Replay a batch of offline-queued mutations.

    Processes each operation in order, returning per-op results so the client
    can remove successful/conflicted ops from its IndexedDB queue.
    """
    household_id = tenant.household_id
    person = tenant.display_name or "Unknown"
    results = []
    ws_events = set()

    try:
        with get_db() as conn:
            for op in body.operations:
                key, match = _match_op(op.url, op.method)
                if not key:
                    results.append({"id": op.id, "status": "error", "error": f"Unrecognized: {op.method} {op.url}"})
                    continue
                try:
                    parsed = _parse_body(op.body)
                    status, extras = _exec_op(conn, key, match, parsed, household_id, person)
                    result = {"id": op.id, "status": status}
                    if extras.get("serverId"):
                        result["serverId"] = extras["serverId"]
                    if extras.get("error"):
                        result["error"] = extras["error"]
                    results.append(result)
                    # Collect broadcast events for after commit
                    if status == "ok":
                        if key.startswith("adhoc_"):
                            ws_events.add("adhoc_updated")
                        elif key.startswith("chore_"):
                            ws_events.add("chore_updated")
                        elif key.startswith("shopping_"):
                            ws_events.add("shopping_updated")
                except sqlite3.Error as e:
                    logger.error("DB error in sync/push op %s: %s", op.id, e)
                    results.append({"id": op.id, "status": "error", "error": str(e)})

        # Broadcast aggregated events
        for event_type in ws_events:
            await manager.broadcast({"type": event_type}, household_id=household_id)

        return {"results": results}

    except sqlite3.Error as e:
        logger.error("Database error in sync_push: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
