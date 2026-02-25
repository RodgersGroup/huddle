import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext
from db import get_db
from settings import get_setting
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()

# ---------------------------------------------------------------------------
# Duty rotation helpers
# ---------------------------------------------------------------------------

DUTY_TYPES_PER_SLOT = ["cook_lunch", "cook_dinner"]
DUTY_TYPES_WEEKLY = ["baking", "shopping"]
ALL_DUTY_TYPES = DUTY_TYPES_PER_SLOT + DUTY_TYPES_WEEKLY


def _get_members_for_rotation(conn, household_id: int) -> list[str]:
    """Get ordered list of household member names for rotation.

    Uses meal_rotation_order setting if set, otherwise falls back to
    household_members ordered by id.
    """
    import json as _json

    # Get actual members from DB (canonical source of truth)
    rows = conn.execute(
        "SELECT display_name FROM household_members WHERE household_id = ? ORDER BY id",
        (household_id,),
    ).fetchall()
    db_members = [r["display_name"] for r in rows]

    # Check for custom order
    raw = conn.execute(
        "SELECT value FROM settings WHERE key = 'meal_rotation_order' AND household_id = ?",
        (household_id,),
    ).fetchone()

    if raw and raw["value"]:
        try:
            custom_order = _json.loads(raw["value"])
        except (ValueError, TypeError):
            custom_order = []

        if custom_order:
            db_set = set(db_members)
            # Keep only members who still exist
            ordered = [m for m in custom_order if m in db_set]
            # Append any new members not in custom order
            for m in db_members:
                if m not in ordered:
                    ordered.append(m)
            return ordered

    return db_members


def _current_week_monday(tz_name: str = "Australia/Sydney") -> str:
    """Return ISO date string of this week's Monday."""
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def _get_excluded_days(conn, household_id: int) -> dict:
    """Get excluded days config. Returns dict of {day_int_str: label}."""
    import json as _json
    raw = conn.execute(
        "SELECT value FROM settings WHERE key = 'meal_excluded_days' AND household_id = ?",
        (household_id,),
    ).fetchone()
    if raw and raw["value"]:
        try:
            return _json.loads(raw["value"])
        except (ValueError, TypeError):
            return {}
    return {}


def _ensure_duty_rows(conn, household_id: int, excluded_days: dict | None = None):
    """Lazily create duty rows if they don't exist yet."""
    excluded = set(int(d) for d in (excluded_days or {}).keys())
    for duty_type in DUTY_TYPES_PER_SLOT:
        for dow in range(7):
            if dow in excluded:
                continue
            # Offset dinner from lunch so different people handle each on the same day
            offset = 3 if duty_type == "cook_dinner" else 0
            conn.execute(
                """INSERT OR IGNORE INTO meal_duties
                   (household_id, duty_type, day_of_week, current_person_index, last_rotated_date)
                   VALUES (?, ?, ?, ?, NULL)""",
                (household_id, duty_type, dow, dow + offset),
            )
    for duty_type in DUTY_TYPES_WEEKLY:
        # Check if row already exists (NULL day_of_week breaks UNIQUE constraint in SQLite)
        existing = conn.execute(
            "SELECT id FROM meal_duties WHERE household_id = ? AND duty_type = ? AND day_of_week IS NULL",
            (household_id, duty_type),
        ).fetchone()
        if not existing:
            idx = 0 if duty_type == "baking" else 1
            conn.execute(
                """INSERT INTO meal_duties
                   (household_id, duty_type, day_of_week, current_person_index, last_rotated_date)
                   VALUES (?, ?, NULL, ?, NULL)""",
                (household_id, duty_type, idx),
            )
    conn.commit()


def _rotate_if_needed(conn, household_id: int, tz_name: str = "Australia/Sydney"):
    """Advance all duty indexes by 1 if a new week has started since last rotation."""
    this_monday = _current_week_monday(tz_name)

    # Check any duty row to see if rotation is needed
    sample = conn.execute(
        "SELECT last_rotated_date FROM meal_duties WHERE household_id = ? LIMIT 1",
        (household_id,),
    ).fetchone()

    if not sample:
        return  # no rows yet

    last = sample["last_rotated_date"]
    if last and last >= this_monday:
        return  # already rotated this week

    # Advance all indexes by 1
    conn.execute(
        """UPDATE meal_duties
           SET current_person_index = current_person_index + 1,
               last_rotated_date = ?
           WHERE household_id = ?""",
        (this_monday, household_id),
    )

    # Reset one-off overrides: restore original index + 1 (for the new week's rotation)
    overrides = conn.execute(
        "SELECT id, override_original_index FROM meal_duties WHERE household_id = ? AND is_override = 1",
        (household_id,),
    ).fetchall()
    for ov in overrides:
        restored = ov["override_original_index"] + 1
        conn.execute(
            "UPDATE meal_duties SET current_person_index = ?, is_override = 0, override_original_index = NULL WHERE id = ? AND household_id = ?",
            (restored, ov["id"], household_id),
        )

    conn.commit()
    logger.info("Meal duties rotated for household %s (week of %s)", household_id, this_monday)


def get_duties_for_household(conn, household_id: int, tz_name: str = "Australia/Sydney") -> dict:
    """Get current duty assignments. Returns dict with cook_lunch, cook_dinner (per day), baking, shopping."""
    excluded_days = _get_excluded_days(conn, household_id)
    excluded_set = set(int(d) for d in excluded_days.keys())

    _ensure_duty_rows(conn, household_id, excluded_days)
    _rotate_if_needed(conn, household_id, tz_name)

    members = _get_members_for_rotation(conn, household_id)
    if not members:
        return {"cook_lunch": {}, "cook_dinner": {}, "baking": None, "shopping": None,
                "excluded_days": excluded_days, "rotation_order": [],
                "week_of": _current_week_monday(tz_name)}

    # Build extended members list that includes all household members
    # (rotation members first, then any others — so non-rotation people can be assigned too)
    all_db_members = [r["display_name"] for r in conn.execute(
        "SELECT display_name FROM household_members WHERE household_id = ? ORDER BY id",
        (household_id,),
    ).fetchall()]
    extended_members = list(members)
    for m in all_db_members:
        if m not in extended_members:
            extended_members.append(m)

    rows = conn.execute(
        "SELECT duty_type, day_of_week, current_person_index FROM meal_duties WHERE household_id = ?",
        (household_id,),
    ).fetchall()

    result = {"cook_lunch": {}, "cook_dinner": {}, "baking": None, "shopping": None}
    n = len(extended_members)

    for row in rows:
        duty = row["duty_type"]
        idx = row["current_person_index"] % n
        person = extended_members[idx]

        if duty in DUTY_TYPES_PER_SLOT:
            dow = row["day_of_week"]
            if dow in excluded_set:
                continue
            result[duty][dow] = person
        elif duty in DUTY_TYPES_WEEKLY:
            result[duty] = person

    result["week_of"] = _current_week_monday(tz_name)
    result["excluded_days"] = excluded_days
    result["rotation_order"] = members
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/meals")
def get_meals(tenant: TenantContext = Depends(get_current_user)):
    """Get current meal plan grouped by day and type, plus duty assignments."""
    household_id = tenant.household_id
    try:
        tz_name = get_setting("timezone", "Australia/Sydney", household_id)
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM meals WHERE household_id = ? ORDER BY day_of_week, meal_type", (household_id,)).fetchall()
            meals = {}
            for row in rows:
                day = row['day_of_week']
                if day not in meals:
                    meals[day] = {'lunch': [], 'dinner': []}
                meal_entry = {
                    'id': row['id'],
                    'meal_name': row['meal_name'],
                    'meal_variant': row['meal_variant'],
                    'cooked_by': row['cooked_by'] if 'cooked_by' in row.keys() else None,
                }
                try:
                    meal_entry['recipe_id'] = row['recipe_id']
                except (IndexError, KeyError):
                    meal_entry['recipe_id'] = None
                meals[day][row['meal_type']].append(meal_entry)

            duties = get_duties_for_household(conn, household_id, tz_name)
            return {"meals": meals, "duties": duties}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/meals/duties")
def get_duties(tenant: TenantContext = Depends(get_current_user)):
    """Get current week's duty assignments."""
    household_id = tenant.household_id
    try:
        tz_name = get_setting("timezone", "Australia/Sydney", household_id)
        with get_db() as conn:
            duties = get_duties_for_household(conn, household_id, tz_name)
            return {"duties": duties}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_duties: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_duties: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/meals/duties/config")
def get_duties_config(tenant: TenantContext = Depends(get_current_user)):
    """Get meal duty rotation configuration."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            members = _get_members_for_rotation(conn, household_id)
            excluded = _get_excluded_days(conn, household_id)
            return {"rotation_order": members, "excluded_days": excluded}
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in get_duties_config: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/meals/duties/config")
async def update_duties_config(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update meal duty rotation configuration.

    Body: { "rotation_order": ["Name1", "Name2"], "excluded_days": {"4": "Takeaway"} }
    Both fields are optional - only provided fields are updated.
    """
    import json as _json
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            if "rotation_order" in data:
                order = data["rotation_order"]
                if not isinstance(order, list):
                    raise HTTPException(status_code=400, detail="rotation_order must be a list")

                db_members = set(r["display_name"] for r in conn.execute(
                    "SELECT display_name FROM household_members WHERE household_id = ?",
                    (household_id,),
                ).fetchall())

                unknown = [n for n in order if n not in db_members]
                if unknown:
                    raise HTTPException(status_code=400, detail="Unknown members: " + ", ".join(unknown))

                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
                    ("meal_rotation_order", _json.dumps(order), household_id),
                )

            if "excluded_days" in data:
                excluded = data["excluded_days"]
                if not isinstance(excluded, dict):
                    raise HTTPException(status_code=400, detail="excluded_days must be an object")

                for key, label in excluded.items():
                    try:
                        day_int = int(key)
                    except ValueError:
                        raise HTTPException(status_code=400, detail="Invalid day key: " + str(key))
                    if not (0 <= day_int <= 6):
                        raise HTTPException(status_code=400, detail="Day must be 0-6, got " + str(day_int))
                    if not isinstance(label, str) or not label.strip():
                        raise HTTPException(status_code=400, detail="Label for day " + str(key) + " must be a non-empty string")
                    if len(label) > 50:
                        raise HTTPException(status_code=400, detail="Label for day " + str(key) + " too long (max 50 chars)")

                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
                    ("meal_excluded_days", _json.dumps(excluded), household_id),
                )

                for key in excluded:
                    conn.execute(
                        "DELETE FROM meal_duties WHERE household_id = ? AND day_of_week = ?",
                        (household_id, int(key)),
                    )

            conn.commit()

        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        return {"message": "Duty configuration updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_duties_config: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_duties_config: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/meals/duties/swap")
async def swap_duty(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Swap a duty assignment for this week (override rotation).

    Body: { "duty_type": "cook_dinner", "day_of_week": 0, "person": "Ciara" }
    For weekly duties (baking/shopping), day_of_week is omitted.
    """
    household_id = tenant.household_id
    try:
        data = await request.json()
        permanent = data.get("permanent", False)
        duty_type = data.get("duty_type", "")
        person = data.get("person", "").strip()
        day_of_week = data.get("day_of_week")

        if duty_type not in ALL_DUTY_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid duty_type: {duty_type}")
        if not person:
            raise HTTPException(status_code=400, detail="person is required")

        with get_db() as conn:
            rotation_members = _get_members_for_rotation(conn, household_id)

            # Build extended members list (rotation + all others) — same as get_duties_for_household
            all_db_members = [r["display_name"] for r in conn.execute(
                "SELECT display_name FROM household_members WHERE household_id = ? ORDER BY id",
                (household_id,),
            ).fetchall()]
            if person not in all_db_members:
                raise HTTPException(status_code=400, detail=f"Unknown member: {person}")

            members = list(rotation_members)
            for m in all_db_members:
                if m not in members:
                    members.append(m)

            target_index = members.index(person)

            # For per-slot duties, need day_of_week
            if duty_type in DUTY_TYPES_PER_SLOT:
                if day_of_week is None or not (0 <= day_of_week <= 6):
                    raise HTTPException(status_code=400, detail="day_of_week (0-6) required for cook duties")

                # Get current index, compute the offset needed
                row = conn.execute(
                    "SELECT current_person_index FROM meal_duties WHERE household_id = ? AND duty_type = ? AND day_of_week = ?",
                    (household_id, duty_type, day_of_week),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Duty not found")

                # Set current_person_index so that idx % len(members) == target_index
                # We need to find a value where value % n == target_index
                # Keep the magnitude similar to current to avoid issues: use target_index directly
                # since rotation only adds 1 per week, we can just set it
                n = len(members)
                current = row["current_person_index"]
                # Preserve the "generation" (how many full rotations have happened) but change the person
                generation = (current // n) * n
                new_index = generation + target_index

                if permanent:
                    conn.execute(
                        "UPDATE meal_duties SET current_person_index = ?, is_override = 0, override_original_index = NULL "
                        "WHERE household_id = ? AND duty_type = ? AND day_of_week = ?",
                        (new_index, household_id, duty_type, day_of_week),
                    )
                else:
                    conn.execute(
                        "UPDATE meal_duties SET current_person_index = ?, is_override = 1, override_original_index = ? "
                        "WHERE household_id = ? AND duty_type = ? AND day_of_week = ?",
                        (new_index, current, household_id, duty_type, day_of_week),
                    )
            else:
                # Weekly duty
                row = conn.execute(
                    "SELECT current_person_index FROM meal_duties WHERE household_id = ? AND duty_type = ? AND day_of_week IS NULL",
                    (household_id, duty_type),
                ).fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Duty not found")

                n = len(members)
                current = row["current_person_index"]
                generation = (current // n) * n
                new_index = generation + target_index

                if permanent:
                    conn.execute(
                        "UPDATE meal_duties SET current_person_index = ?, is_override = 0, override_original_index = NULL "
                        "WHERE household_id = ? AND duty_type = ? AND day_of_week IS NULL",
                        (new_index, household_id, duty_type),
                    )
                else:
                    conn.execute(
                        "UPDATE meal_duties SET current_person_index = ?, is_override = 1, override_original_index = ? "
                        "WHERE household_id = ? AND duty_type = ? AND day_of_week IS NULL",
                        (new_index, current, household_id, duty_type),
                    )
            conn.commit()

        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        return {"message": f"{duty_type} swapped to {person}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in swap_duty: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in swap_duty: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/meals")
async def save_meals(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Save the entire week's meal plan (replaces existing)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        meals = data.get('meals', [])
        if not isinstance(meals, list):
            logger.warning("Validation failed in save_meals: %s", "meals must be a list")
            raise HTTPException(status_code=400, detail="meals must be a list")
        for i, meal in enumerate(meals):
            meal_name = meal.get('meal_name', '') if isinstance(meal, dict) else ''
            if isinstance(meal_name, str) and len(meal_name) > 200:
                logger.warning("Validation failed in save_meals: %s", f"meal_name is too long at index {i} (max 200 characters)")
                raise HTTPException(status_code=400, detail=f"meal_name is too long at index {i} (max 200 characters)")
        # --- End validation ---

        with get_db() as conn:
            conn.execute("DELETE FROM meals WHERE household_id = ?", (household_id,))
            for meal in data.get('meals', []):
                conn.execute("""
                    INSERT INTO meals (day_of_week, meal_type, meal_name, meal_variant, cooked_by, household_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    meal['day_of_week'],
                    meal['meal_type'],
                    meal['meal_name'],
                    meal.get('meal_variant', 'all'),
                    meal.get('cooked_by'),
                    household_id
                ))
            conn.commit()

        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        return {"message": "Meals saved"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in save_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in save_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/meals/{meal_id}/assign")
async def assign_meal_cook(meal_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Assign or clear a cook for a specific meal. Body: { "cooked_by": "PersonName" } or { "cooked_by": null }"""
    household_id = tenant.household_id
    try:
        data = await request.json()
        cooked_by = data.get('cooked_by')
        if cooked_by is not None:
            cooked_by = cooked_by.strip() if isinstance(cooked_by, str) else ''

        with get_db() as conn:
            meal = conn.execute("SELECT id FROM meals WHERE id = ? AND household_id = ?", (meal_id, household_id)).fetchone()
            if not meal:
                raise HTTPException(status_code=404, detail="Meal not found")
            conn.execute("UPDATE meals SET cooked_by = ? WHERE id = ? AND household_id = ?", (cooked_by or None, meal_id, household_id))
            conn.commit()

        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        return {"message": f"Cook assigned" if cooked_by else "Cook cleared"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in assign_meal_cook: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in assign_meal_cook: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/meals")
async def clear_meals(tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Clear all meals."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM meals WHERE household_id = ?", (household_id,))
            conn.commit()
        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        return {"message": "Meals cleared"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in clear_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in clear_meals: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
