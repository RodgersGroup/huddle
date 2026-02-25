import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime
from db import get_db
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


@router.get("/api/inventory")
def get_inventory(tenant: TenantContext = Depends(get_current_user)):
    """Get all inventory items grouped by category."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM inventory_items WHERE household_id = ? ORDER BY category, name", (household_id,)).fetchall()
            items = [dict(r) for r in rows]
            low_stock = [i for i in items if i['quantity'] <= i['low_threshold']]
            return {"items": items, "low_stock": low_stock}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/inventory")
async def create_inventory_item(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add an inventory item."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_inventory_item: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_inventory_item: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
        data['name'] = name

        quantity = data.get('quantity', 1)
        if quantity is not None:
            try:
                quantity = int(quantity)
                if quantity < 0 or quantity > 999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in create_inventory_item: %s", "Quantity must be a valid integer (0 to 999999)")
                raise HTTPException(status_code=400, detail="Quantity must be a valid integer (0 to 999999)")
            data['quantity'] = quantity

        low_threshold = data.get('low_threshold', 1)
        if low_threshold is not None:
            try:
                low_threshold = int(low_threshold)
                if low_threshold < 0 or low_threshold > 999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in create_inventory_item: %s", "low_threshold must be a valid integer (0 to 999999)")
                raise HTTPException(status_code=400, detail="low_threshold must be a valid integer (0 to 999999)")
            data['low_threshold'] = low_threshold
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO inventory_items (name, category, quantity, unit, low_threshold, added_by, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                data['name'],
                data.get('category', 'other'),
                data.get('quantity', 1),
                data.get('unit'),
                data.get('low_threshold', 1),
                data.get('added_by'),
                household_id
            ))
            conn.commit()
            item_id = cursor.lastrowid
        await manager.broadcast({"type": "inventory_updated"}, household_id=household_id)
        return {"id": item_id, "message": "Item added"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/inventory/{item_id}")
async def update_inventory_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update an inventory item."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        if 'name' in data:
            name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
            if not name:
                logger.warning("Validation failed in update_inventory_item: %s", "Name cannot be empty")
                raise HTTPException(status_code=400, detail="Name cannot be empty")
            if len(name) > 200:
                logger.warning("Validation failed in update_inventory_item: %s", "Name is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
            data['name'] = name

        if 'quantity' in data and data['quantity'] is not None:
            try:
                quantity = int(data['quantity'])
                if quantity < 0 or quantity > 999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_inventory_item: %s", "Quantity must be a valid integer (0 to 999999)")
                raise HTTPException(status_code=400, detail="Quantity must be a valid integer (0 to 999999)")
            data['quantity'] = quantity

        if 'low_threshold' in data and data['low_threshold'] is not None:
            try:
                low_threshold = int(data['low_threshold'])
                if low_threshold < 0 or low_threshold > 999999:
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning("Validation failed in update_inventory_item: %s", "low_threshold must be a valid integer (0 to 999999)")
                raise HTTPException(status_code=400, detail="low_threshold must be a valid integer (0 to 999999)")
            data['low_threshold'] = low_threshold
        # --- End validation ---

        with get_db() as conn:
            existing = conn.execute("SELECT * FROM inventory_items WHERE id = ? AND household_id = ?", (item_id, household_id)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")
            conn.execute("""
                UPDATE inventory_items SET name=?, category=?, quantity=?, unit=?, low_threshold=?, updated_at=?
                WHERE id=? AND household_id=?
            """, (
                data.get('name', existing['name']),
                data.get('category', existing['category']),
                data.get('quantity', existing['quantity']),
                data.get('unit', existing['unit']),
                data.get('low_threshold', existing['low_threshold']),
                datetime.now().isoformat(),
                item_id,
                household_id
            ))
            conn.commit()
        await manager.broadcast({"type": "inventory_updated"}, household_id=household_id)
        return {"message": "Item updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/inventory/{item_id}")
async def delete_inventory_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Remove an inventory item."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("DELETE FROM inventory_items WHERE id = ? AND household_id = ?", (item_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "inventory_updated"}, household_id=household_id)
        return {"message": "Item deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_inventory_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/inventory/{item_id}/increment")
async def increment_inventory(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Quick +1 quantity."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("""
                UPDATE inventory_items SET quantity = quantity + 1, updated_at = ? WHERE id = ? AND household_id = ?
            """, (datetime.now().isoformat(), item_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "inventory_updated"}, household_id=household_id)
        return {"message": "Quantity incremented"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in increment_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in increment_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/inventory/{item_id}/decrement")
async def decrement_inventory(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Quick -1 quantity (minimum 0)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("""
                UPDATE inventory_items SET quantity = MAX(0, quantity - 1), updated_at = ? WHERE id = ? AND household_id = ?
            """, (datetime.now().isoformat(), item_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "inventory_updated"}, household_id=household_id)
        return {"message": "Quantity decremented"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in decrement_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in decrement_inventory: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
