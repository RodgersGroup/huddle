"""Routines API — morning/bedtime checklists with daily reset and streaks."""
import logging
import sqlite3
import json
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from auth import get_current_user, require_role, TenantContext
from db import get_db, check_version
from settings import get_setting
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


def _get_today_str(household_id):
    """Get today's date string in the household's timezone."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz_name = get_setting("timezone", "Australia/Sydney", household_id=household_id)
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _get_streak(conn, routine_id, household_id):
    """Count consecutive days where ALL items in a routine were completed.

    Uses a single query to fetch all fully-completed dates in the last year,
    then counts consecutive days backwards from today in Python.
    """
    items = conn.execute(
        "SELECT id FROM routine_items WHERE routine_id = ?", (routine_id,)
    ).fetchall()
    if not items:
        return 0
    item_count = len(items)

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    tz_name = get_setting("timezone", "Australia/Sydney", household_id=household_id)
    today = datetime.now(ZoneInfo(tz_name)).date()

    # Single query: fetch dates with all items completed (last 366 days)
    cutoff = (today - timedelta(days=366)).isoformat()
    rows = conn.execute(
        """SELECT completed_date FROM routine_completions
           WHERE routine_id = ? AND completed_date >= ?
           GROUP BY completed_date
           HAVING COUNT(DISTINCT item_id) >= ?""",
        (routine_id, cutoff, item_count)
    ).fetchall()
    completed_dates = {r["completed_date"] for r in rows}

    streak = 0

    # Check today first (may be incomplete)
    if today.isoformat() in completed_dates:
        streak = 1

    # Count backwards from yesterday
    check_date = today - timedelta(days=1)
    for _ in range(365):
        if check_date.isoformat() in completed_dates:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break

    return streak


@router.get("/api/routines")
def get_routines(tenant: TenantContext = Depends(get_current_user), since: str | None = Query(None)):
    """Get all routines with items and today's completions. Pass ?since=<timestamp> for delta sync."""
    household_id = tenant.household_id
    try:
        today_str = _get_today_str(household_id)

        with get_db() as conn:
            if since:
                routines = conn.execute(
                    "SELECT * FROM routines WHERE household_id = ? AND deleted_at IS NULL AND updated_at > ? ORDER BY sort_order, id",
                    (household_id, since),
                ).fetchall()
            else:
                routines = conn.execute(
                    "SELECT * FROM routines WHERE household_id = ? AND deleted_at IS NULL ORDER BY sort_order, id",
                    (household_id,),
                ).fetchall()

            result = []
            for r in routines:
                items = conn.execute(
                    "SELECT * FROM routine_items WHERE routine_id = ? ORDER BY sort_order, id",
                    (r["id"],)
                ).fetchall()

                # Get today's completions for this routine
                completions = conn.execute(
                    "SELECT item_id FROM routine_completions WHERE routine_id = ? AND completed_date = ?",
                    (r["id"], today_str)
                ).fetchall()
                completed_item_ids = {c["item_id"] for c in completions}

                item_list = []
                for item in items:
                    item_list.append({
                        "id": item["id"],
                        "name": item["name"],
                        "icon": item["icon"],
                        "sort_order": item["sort_order"],
                        "completed_today": item["id"] in completed_item_ids,
                    })

                done = len(completed_item_ids)
                total = len(items)

                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "routine_type": r["routine_type"],
                    "assigned_to": r["assigned_to"],
                    "sort_order": r["sort_order"],
                    "items": item_list,
                    "progress": {"done": done, "total": total},
                    "all_done": done >= total and total > 0,
                    "streak": _get_streak(conn, r["id"], household_id),
                })

            response = {"routines": result}
            if since:
                deleted_rows = conn.execute(
                    "SELECT id FROM routines WHERE household_id = ? AND deleted_at > ?",
                    (household_id, since),
                ).fetchall()
                response["deleted"] = [r["id"] for r in deleted_rows]
            return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_routines: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_routines: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/routines")
async def create_routine(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a routine with items."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get("name", "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Routine name is required")
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="Name too long (max 200)")
        assigned_to = data.get("assigned_to", "").strip()
        if not assigned_to:
            raise HTTPException(status_code=400, detail="assigned_to is required")
        routine_type = data.get("routine_type", "morning")
        if routine_type not in ("morning", "afternoon", "bedtime", "custom"):
            raise HTTPException(status_code=400, detail="Invalid routine_type")
        items = data.get("items", [])
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="items must be a list")
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO routines (name, routine_type, assigned_to, sort_order, household_id) VALUES (?, ?, ?, ?, ?)",
                (name, routine_type, assigned_to, data.get("sort_order", 0), household_id)
            )
            routine_id = cursor.lastrowid

            for i, item in enumerate(items):
                item_name = item.get("name", "").strip() if isinstance(item, dict) else str(item).strip()
                if not item_name:
                    continue
                icon = item.get("icon", "✓") if isinstance(item, dict) else "✓"
                conn.execute(
                    "INSERT INTO routine_items (routine_id, name, icon, sort_order) VALUES (?, ?, ?, ?)",
                    (routine_id, item_name, icon, i)
                )

            conn.commit()

        await manager.broadcast({"type": "routines_updated"}, household_id=household_id)
        return {"id": routine_id, "message": "Routine created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/routines/{routine_id}")
async def update_routine(routine_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a routine and its items."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            routine = conn.execute(
                "SELECT * FROM routines WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (routine_id, household_id)
            ).fetchone()
            if not routine:
                raise HTTPException(status_code=404, detail="Routine not found")
            check_version(data, routine, "routine")

            # Update routine fields
            name = data.get("name", routine["name"]).strip()
            routine_type = data.get("routine_type", routine["routine_type"])
            assigned_to = data.get("assigned_to", routine["assigned_to"])
            sort_order = data.get("sort_order", routine["sort_order"])

            conn.execute(
                "UPDATE routines SET name = ?, routine_type = ?, assigned_to = ?, sort_order = ?, version = COALESCE(version, 0) + 1, updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (name, routine_type, assigned_to, sort_order, routine_id, household_id)
            )

            # Replace items if provided
            if "items" in data:
                conn.execute("DELETE FROM routine_items WHERE routine_id = ?", (routine_id,))
                for i, item in enumerate(data["items"]):
                    item_name = item.get("name", "").strip() if isinstance(item, dict) else str(item).strip()
                    if not item_name:
                        continue
                    icon = item.get("icon", "✓") if isinstance(item, dict) else "✓"
                    conn.execute(
                        "INSERT INTO routine_items (routine_id, name, icon, sort_order) VALUES (?, ?, ?, ?)",
                        (routine_id, item_name, icon, i)
                    )

            conn.commit()

        await manager.broadcast({"type": "routines_updated"}, household_id=household_id)
        return {"message": "Routine updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/routines/{routine_id}")
async def delete_routine(routine_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a routine and all its items/completions."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            routine = conn.execute(
                "SELECT id FROM routines WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (routine_id, household_id)
            ).fetchone()
            if not routine:
                raise HTTPException(status_code=404, detail="Routine not found")

            conn.execute("UPDATE routines SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (routine_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "routines_updated"}, household_id=household_id)
        return {"message": "Routine deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_routine: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/routines/{routine_id}/items/{item_id}/complete")
async def complete_routine_item(routine_id: int, item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Mark a routine item as done for today."""
    household_id = tenant.household_id
    try:
        today_str = _get_today_str(household_id)

        with get_db() as conn:
            # Verify routine and item exist
            routine = conn.execute(
                "SELECT * FROM routines WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (routine_id, household_id)
            ).fetchone()
            if not routine:
                raise HTTPException(status_code=404, detail="Routine not found")

            item = conn.execute(
                "SELECT * FROM routine_items WHERE id = ? AND routine_id = ?",
                (item_id, routine_id)
            ).fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="Item not found")

            # Check if already completed today
            existing = conn.execute(
                "SELECT id FROM routine_completions WHERE item_id = ? AND completed_date = ?",
                (item_id, today_str)
            ).fetchone()
            if existing:
                return {"message": "Already completed today", "already_done": True}

            conn.execute(
                "INSERT INTO routine_completions (routine_id, item_id, completed_by, completed_date, household_id) VALUES (?, ?, ?, ?, ?)",
                (routine_id, item_id, tenant.display_name, today_str, household_id)
            )
            conn.commit()

            # Check if all items are now done
            total_items = conn.execute(
                "SELECT COUNT(*) FROM routine_items WHERE routine_id = ?", (routine_id,)
            ).fetchone()[0]
            completed_items = conn.execute(
                "SELECT COUNT(*) FROM routine_completions WHERE routine_id = ? AND completed_date = ?",
                (routine_id, today_str)
            ).fetchone()[0]

            all_done = completed_items >= total_items
            result = {
                "message": "Item completed",
                "progress": {"done": completed_items, "total": total_items},
                "all_done": all_done,
            }

            # If all done, send push to managers
            if all_done:
                try:
                    from push import send_push_to_person_bg, PUSH_ENABLED
                    from settings import get_people
                    if PUSH_ENABLED:
                        type_emoji = {"morning": "☀️", "bedtime": "🌙", "afternoon": "🌤️"}.get(routine["routine_type"], "✅")
                        people = get_people(household_id=household_id)
                        # Send to all people (managers will receive it)
                        for person in people:
                            if person != routine["assigned_to"]:
                                send_push_to_person_bg(
                                    person,
                                    f"Huddle: Routine complete!",
                                    f"{routine['assigned_to']} finished {routine['name']}",
                                    "routine-complete",
                                    household_id=household_id,
                                )
                except Exception as e:
                    logger.warning("Push notification failed for routine completion: %s", e)

                result["streak"] = _get_streak(conn, routine_id, household_id)

                # Award bonus stars for completing full routine (if rewards module enabled)
                try:
                    mod_enabled = conn.execute(
                        "SELECT enabled FROM household_modules WHERE household_id = ? AND module_key = 'rewards'",
                        (household_id,)
                    ).fetchone()
                    if mod_enabled and mod_enabled["enabled"]:
                        conn.execute(
                            "INSERT INTO reward_points (person, points, reason, source_type, source_id, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (routine["assigned_to"], 3, f"Completed routine: {routine['name']}", "routine", routine_id, household_id)
                        )
                        conn.commit()
                except Exception as e:
                    logger.warning("Failed to award routine completion points for %s: %s", routine["assigned_to"], e)

        await manager.broadcast({"type": "routines_updated"}, household_id=household_id)
        return result
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_routine_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_routine_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/routines/{routine_id}/items/{item_id}/uncomplete")
async def uncomplete_routine_item(routine_id: int, item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Undo a routine item completion for today."""
    household_id = tenant.household_id
    try:
        today_str = _get_today_str(household_id)

        with get_db() as conn:
            routine = conn.execute(
                "SELECT id FROM routines WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (routine_id, household_id)
            ).fetchone()
            if not routine:
                raise HTTPException(status_code=404, detail="Routine not found")

            conn.execute(
                "DELETE FROM routine_completions WHERE item_id = ? AND completed_date = ? AND routine_id = ?",
                (item_id, today_str, routine_id)
            )
            conn.commit()

        await manager.broadcast({"type": "routines_updated"}, household_id=household_id)
        return {"message": "Completion undone"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in uncomplete_routine_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in uncomplete_routine_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/routines/streaks")
def get_routine_streaks(tenant: TenantContext = Depends(get_current_user)):
    """Get streak data per person per routine."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            routines = conn.execute(
                "SELECT id, name, assigned_to, routine_type FROM routines WHERE household_id = ? AND deleted_at IS NULL",
                (household_id,)
            ).fetchall()

            streaks = []
            for r in routines:
                streak = _get_streak(conn, r["id"], household_id)
                streaks.append({
                    "routine_id": r["id"],
                    "routine_name": r["name"],
                    "person": r["assigned_to"],
                    "routine_type": r["routine_type"],
                    "streak": streak,
                })

            return {"streaks": streaks}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_routine_streaks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_routine_streaks: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
