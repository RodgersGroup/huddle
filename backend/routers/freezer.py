"""Freezer inventory — track freezer meals, leftovers, and frozen items."""

import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from auth import get_current_user, TenantContext
from db import get_db, check_version
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/freezer")
def list_freezer_items(tenant: TenantContext = Depends(get_current_user), since: str | None = Query(None)):
    """List all freezer items for the household. Pass ?since=<timestamp> for delta sync."""
    try:
        with get_db() as conn:
            if since:
                rows = conn.execute(
                    "SELECT * FROM freezer_items WHERE household_id = ? AND deleted_at IS NULL AND updated_at > ? ORDER BY date_frozen DESC, created_at DESC",
                    (tenant.household_id, since),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM freezer_items WHERE household_id = ? AND deleted_at IS NULL ORDER BY date_frozen DESC, created_at DESC",
                    (tenant.household_id,),
                ).fetchall()
            response = {"items": [dict(r) for r in rows]}
            if since:
                deleted_rows = conn.execute(
                    "SELECT id FROM freezer_items WHERE household_id = ? AND deleted_at > ?",
                    (tenant.household_id, since),
                ).fetchall()
                response["deleted"] = [r["id"] for r in deleted_rows]
            return response
    except sqlite3.Error as e:
        logger.error("Database error in list_freezer_items: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/api/freezer")
async def create_freezer_item(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add an item to the freezer."""
    try:
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO freezer_items (name, category, quantity, date_frozen, expiry_date, notes, added_by, household_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    data.get("category", "meal"),
                    data.get("quantity", 1),
                    data.get("date_frozen", ""),
                    data.get("expiry_date", ""),
                    data.get("notes", ""),
                    tenant.display_name,
                    tenant.household_id,
                ),
            )
            conn.commit()

        await manager.broadcast({"type": "inventory_updated"}, household_id=tenant.household_id)
        return {"id": cursor.lastrowid, "message": "Item added to freezer"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/freezer/{item_id}")
async def update_freezer_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a freezer item."""
    try:
        data = await request.json()
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM freezer_items WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (item_id, tenant.household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")
            check_version(data, existing, "freezer item")

            conn.execute(
                """UPDATE freezer_items SET name=?, category=?, quantity=?, date_frozen=?, expiry_date=?, notes=?,
                       version = COALESCE(version, 0) + 1, updated_at = datetime('now')
                   WHERE id=? AND household_id=?""",
                (
                    data.get("name", ""),
                    data.get("category", "meal"),
                    data.get("quantity", 1),
                    data.get("date_frozen", ""),
                    data.get("expiry_date", ""),
                    data.get("notes", ""),
                    item_id,
                    tenant.household_id,
                ),
            )
            conn.commit()

        await manager.broadcast({"type": "inventory_updated"}, household_id=tenant.household_id)
        return {"message": "Item updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/freezer/{item_id}")
async def delete_freezer_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a freezer item."""
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE freezer_items SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (item_id, tenant.household_id),
            )
            conn.commit()

        await manager.broadcast({"type": "inventory_updated"}, household_id=tenant.household_id)
        return {"message": "Item removed from freezer"}
    except sqlite3.Error as e:
        logger.error("Database error in delete_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_freezer_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
