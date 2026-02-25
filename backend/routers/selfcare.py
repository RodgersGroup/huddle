"""Self Care module — medication tracking and personal care reminders."""

import logging
import sqlite3
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user, TenantContext
from db import get_db
from settings import get_setting
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()

VALID_CATEGORIES = ("medication", "personal_care", "life_admin")


def _today_local(household_id: int) -> date:
    tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
    return datetime.now(tz).date()


STOCK_FIELDS = ("box_quantity", "box_total", "repeats_remaining", "repeats_total", "dose_quantity")


def _compute_stock_alerts(item: dict) -> list[dict]:
    """Return a list of alert dicts for medication stock levels.

    Each alert: {"type": "restock"|"chemist"|"gp", "message": str, "severity": "warning"|"urgent"}
    """
    alerts: list[dict] = []
    bq = item.get("box_quantity")
    bt = item.get("box_total")
    rr = item.get("repeats_remaining")
    dose = item.get("dose_quantity") or 1
    freq = item.get("frequency_days") or 0

    if bq is None:
        return alerts  # stock tracking not configured

    # Calculate how many days of medication remain
    if freq > 0 and dose > 0:
        doses_left = bq / dose
        days_remaining = doses_left * freq
    else:
        doses_left = bq / dose if dose > 0 else bq
        days_remaining = None  # as-needed, can't predict

    # GP alert: repeats running out — need a new prescription
    if rr is not None and rr <= 1:
        if rr == 0:
            alerts.append({"type": "gp", "message": "No repeats left — book GP for new script", "severity": "urgent"})
        else:
            alerts.append({"type": "gp", "message": "Last repeat — book GP soon", "severity": "warning"})

    # Chemist alert: box empty or nearly empty and repeats available
    if bq <= 0 and rr is not None and rr > 0:
        alerts.append({"type": "chemist", "message": "Box empty — go to chemist for refill", "severity": "urgent"})
    elif bq <= 0 and (rr is None or rr == 0):
        alerts.append({"type": "restock", "message": "Out of stock", "severity": "urgent"})
    elif days_remaining is not None and days_remaining <= 7:
        pills_word = f"{bq} left"
        day_word = f"{int(days_remaining)}d" if days_remaining >= 1 else "today"
        alerts.append({"type": "restock", "message": f"Running low — {pills_word} (~{day_word})", "severity": "warning"})
    elif days_remaining is not None and days_remaining <= 14:
        alerts.append({"type": "restock", "message": f"Restock soon — ~{int(days_remaining)} days left", "severity": "info"})

    return alerts


@router.get("/api/selfcare")
def get_selfcare_items(tenant: TenantContext = Depends(get_current_user)):
    """List all self care items with last-logged info."""
    household_id = tenant.household_id
    try:
        today = _today_local(household_id)
        with get_db() as conn:
            items = conn.execute(
                "SELECT * FROM selfcare_items WHERE household_id = ? ORDER BY category, name",
                (household_id,),
            ).fetchall()

            result = []
            for item in items:
                item_dict = dict(item)
                # Get last log
                last_log = conn.execute(
                    "SELECT logged_at FROM selfcare_logs WHERE item_id = ? AND household_id = ? ORDER BY logged_at DESC LIMIT 1",
                    (item["id"], household_id),
                ).fetchone()
                # Get log count
                log_count = conn.execute(
                    "SELECT COUNT(*) FROM selfcare_logs WHERE item_id = ? AND household_id = ?",
                    (item["id"], household_id),
                ).fetchone()[0]

                item_dict["last_logged_at"] = last_log["logged_at"] if last_log else None
                item_dict["log_count"] = log_count

                if last_log:
                    last_date = datetime.fromisoformat(last_log["logged_at"]).date()
                    item_dict["days_since"] = (today - last_date).days
                else:
                    item_dict["days_since"] = None

                freq = item["frequency_days"]
                if freq > 0:
                    if item_dict["days_since"] is None:
                        item_dict["is_due"] = True
                    else:
                        item_dict["is_due"] = item_dict["days_since"] >= freq
                else:
                    item_dict["is_due"] = False  # as-needed items are never "due"

                # Stock tracking alerts for medication items
                item_dict["stock_alerts"] = _compute_stock_alerts(item_dict)

                result.append(item_dict)

        return {"items": result}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_selfcare_items: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_selfcare_items: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/selfcare")
async def create_selfcare_item(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Create a self care item."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="Name too long")

        category = data.get("category", "medication")
        if category not in VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail="Invalid category")

        assigned_to = data.get("assigned_to", "").strip()
        if not assigned_to:
            raise HTTPException(status_code=400, detail="Assigned to is required")

        frequency_days = int(data.get("frequency_days", 0))
        if frequency_days < 0:
            frequency_days = 0

        reminder_enabled = 1 if data.get("reminder_enabled") else 0
        notes = (data.get("notes") or "").strip()[:500]
        icon = (data.get("icon") or "").strip()[:10]

        # Stock tracking fields (medication)
        box_quantity = data.get("box_quantity")
        box_total = data.get("box_total")
        repeats_remaining = data.get("repeats_remaining")
        repeats_total = data.get("repeats_total")
        dose_quantity = data.get("dose_quantity")
        if box_quantity is not None:
            box_quantity = max(0, int(box_quantity))
        if box_total is not None:
            box_total = max(1, int(box_total))
        if repeats_remaining is not None:
            repeats_remaining = max(0, int(repeats_remaining))
        if repeats_total is not None:
            repeats_total = max(0, int(repeats_total))
        if dose_quantity is not None:
            dose_quantity = max(1, int(dose_quantity))

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO selfcare_items
                   (name, category, assigned_to, frequency_days, reminder_enabled, notes, icon,
                    box_quantity, box_total, repeats_remaining, repeats_total, dose_quantity, household_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, category, assigned_to, frequency_days, reminder_enabled, notes or None, icon or None,
                 box_quantity, box_total, repeats_remaining, repeats_total, dose_quantity, household_id),
            )
            conn.commit()
            item_id = cursor.lastrowid

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"id": item_id, "message": "Item created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/selfcare/{item_id}")
async def update_selfcare_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a self care item."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM selfcare_items WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            updates = []
            params = []
            for field in ("name", "category", "assigned_to", "notes", "icon"):
                if field in data:
                    val = (data[field] or "").strip()
                    if field == "name" and not val:
                        raise HTTPException(status_code=400, detail="Name is required")
                    if field == "category" and val not in VALID_CATEGORIES:
                        raise HTTPException(status_code=400, detail="Invalid category")
                    updates.append(f"{field} = ?")
                    params.append(val or None)
            if "frequency_days" in data:
                updates.append("frequency_days = ?")
                params.append(max(0, int(data["frequency_days"])))
            if "reminder_enabled" in data:
                updates.append("reminder_enabled = ?")
                params.append(1 if data["reminder_enabled"] else 0)
            # Stock tracking fields
            for sf in STOCK_FIELDS:
                if sf in data:
                    val = data[sf]
                    if val is not None and val != "":
                        val = int(val)
                        if sf == "dose_quantity":
                            val = max(1, val)
                        elif sf == "box_total":
                            val = max(1, val)
                        else:
                            val = max(0, val)
                    else:
                        val = None
                    updates.append(f"{sf} = ?")
                    params.append(val)

            if updates:
                params.append(item_id)
                params.append(household_id)
                conn.execute(
                    f"UPDATE selfcare_items SET {', '.join(updates)} WHERE id = ? AND household_id = ?",
                    params,
                )
                conn.commit()

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"message": "Item updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/selfcare/{item_id}")
async def delete_selfcare_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a self care item and its logs."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM selfcare_items WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            conn.execute("DELETE FROM selfcare_items WHERE id = ? AND household_id = ?", (item_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"message": "Item deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/selfcare/{item_id}/refill")
async def refill_selfcare_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Refill stock: reset box_quantity to box_total and decrement repeats."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM selfcare_items WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            bt = existing["box_total"]
            if bt is None:
                raise HTTPException(status_code=400, detail="No box total configured")

            rr = existing["repeats_remaining"]
            updates = ["box_quantity = ?"]
            params: list = [bt]
            if rr is not None and rr > 0:
                updates.append("repeats_remaining = ?")
                params.append(rr - 1)

            params.extend([item_id, household_id])
            conn.execute(
                f"UPDATE selfcare_items SET {', '.join(updates)} WHERE id = ? AND household_id = ?",
                params,
            )
            conn.commit()

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"message": "Refilled"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in refill_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in refill_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/selfcare/{item_id}/log")
async def log_selfcare_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Record a completion log for a self care item."""
    household_id = tenant.household_id
    try:
        data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        notes = (data.get("notes") or "").strip()[:500]

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM selfcare_items WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            conn.execute(
                "INSERT INTO selfcare_logs (item_id, logged_by, logged_at, notes, household_id) VALUES (?, ?, ?, ?, ?)",
                (item_id, tenant.display_name, datetime.now().isoformat(), notes or None, household_id),
            )

            # Decrement stock if tracked
            box_qty = existing["box_quantity"]
            if box_qty is not None:
                dose = existing["dose_quantity"] or 1
                new_qty = max(0, box_qty - dose)
                conn.execute(
                    "UPDATE selfcare_items SET box_quantity = ? WHERE id = ? AND household_id = ?",
                    (new_qty, item_id, household_id),
                )

            conn.commit()

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"message": "Logged"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in log_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in log_selfcare_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/selfcare/logs/{log_id}")
async def delete_selfcare_log(log_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Remove an accidental log entry."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM selfcare_logs WHERE id = ? AND household_id = ?",
                (log_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Log not found")

            conn.execute("DELETE FROM selfcare_logs WHERE id = ? AND household_id = ?", (log_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "selfcare_updated"}, household_id=household_id)
        return {"message": "Log deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_selfcare_log: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_selfcare_log: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/selfcare/{item_id}/history")
def get_selfcare_history(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get log history for a self care item."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM selfcare_items WHERE id = ? AND household_id = ?",
                (item_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            logs = conn.execute(
                "SELECT * FROM selfcare_logs WHERE item_id = ? AND household_id = ? ORDER BY logged_at DESC LIMIT 50",
                (item_id, household_id),
            ).fetchall()

        return {"logs": [dict(r) for r in logs]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_selfcare_history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_selfcare_history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
