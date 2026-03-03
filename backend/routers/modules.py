import json
import logging
import sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from db import get_db
from settings import get_setting
from websocket import manager
from tiers import MODULES as TIER_MODULES, is_module_available, SEGMENTS, TIERS

logger = logging.getLogger("huddle")

router = APIRouter()

SUPERADMIN_EMAIL = "keiran@rodgersgroup.au"
_beta_household_ids = None
_beta_module_keys = None


def _get_beta_household_ids():
    """Get household IDs that have beta access (superadmin's households). Cached after first call."""
    global _beta_household_ids
    if _beta_household_ids is not None:
        return _beta_household_ids
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT hm.household_id FROM household_members hm
                   JOIN users u ON u.id = hm.user_id
                   WHERE u.email = ?""",
                (SUPERADMIN_EMAIL,)
            ).fetchall()
            _beta_household_ids = {r["household_id"] for r in rows}
    except Exception:
        _beta_household_ids = set()
    return _beta_household_ids


def _get_beta_module_keys():
    """Get module keys that are in beta. Cached after first call; call _clear_beta_cache() to reset."""
    global _beta_module_keys
    if _beta_module_keys is not None:
        return _beta_module_keys
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT module_key FROM module_beta").fetchall()
            _beta_module_keys = {r["module_key"] for r in rows}
    except Exception:
        _beta_module_keys = set()
    return _beta_module_keys


def _clear_beta_cache():
    """Clear cached beta module keys (call after toggling beta status)."""
    global _beta_module_keys
    _beta_module_keys = None


# All available modules with metadata
AVAILABLE_MODULES = {
    "chores": {
        "name": "Chores",
        "description": "Recurring chore scheduling with rotation between household members",

        "icon": "broom",
    },
    "adhoc_tasks": {
        "name": "Tasks",
        "description": "One-off tasks and requests that can be assigned to members",

        "icon": "clipboard-list",
    },
    "meals": {
        "name": "Meal Planning",
        "description": "Weekly meal planner with per-person dinner variants",

        "icon": "utensils",
    },
    "calendar": {
        "name": "Calendar",
        "description": "Shared family calendar with recurring events and multi-day support",

        "icon": "calendar",
    },
    "bills": {
        "name": "Bills",
        "description": "Track bills and due dates with recurring bill support",

        "icon": "receipt",
    },
    "inventory": {
        "name": "Inventory",
        "description": "Track pantry and household items with low-stock alerts",

        "icon": "boxes-stacked",
    },
    "polls": {
        "name": "Polls",
        "description": "Quick household polls and voting",

        "icon": "square-poll-vertical",
    },
    "fuel": {
        "name": "Fuel Prices",
        "description": "Local fuel price comparison (Australia only)",

        "icon": "gas-pump",
    },
    "weather": {
        "name": "Weather",
        "description": "Local weather forecast on kiosk display",

        "icon": "cloud-sun",
    },
    "shopping": {
        "name": "Shopping List",
        "description": "Shared shopping list with categories",

        "icon": "cart-shopping",
    },
    "recipes": {
        "name": "Recipes",
        "description": "Recipe book with URL import and meal plan integration",

        "icon": "book-open",
    },
    "routines": {
        "name": "Routines",
        "description": "Morning and bedtime checklists that reset daily",

        "icon": "sun",
    },
    "rewards": {
        "name": "Rewards & Stars",
        "description": "Earn stars for completing tasks, redeem for rewards",

        "icon": "star",
    },
    "allowances": {
        "name": "Allowances",
        "description": "Pocket money tracking with savings goals",

        "icon": "piggy-bank",
    },
    "pets": {
        "name": "Pets",
        "description": "Pet care schedules, feeding rotation, vet appointments",

        "icon": "paw-print",
    },
    "noticeboard": {
        "name": "Noticeboard",
        "description": "Household announcements, going out notices, guest alerts",

        "icon": "clipboard",
    },
    "assignments": {
        "name": "Assignments",
        "description": "Track homework, school projects, and assignments with due dates",

        "icon": "graduation-cap",
    },
    "feedback": {
        "name": "Feedback",
        "description": "Submit feedback and suggestions",

        "icon": "message-square",
    },
    "expenses": {
        "name": "Expenses",
        "description": "Split expenses, track balances, and settle debts between housemates",

        "icon": "receipt",
    },
    "house_rules": {
        "name": "House Rules",
        "description": "Shared household agreements that everyone acknowledges",

        "icon": "scroll",
    },
    "tenancy": {
        "name": "Tenancy",
        "description": "Lease details, bond tracking, and move-in/move-out checklists",

        "icon": "house",
    },
    "vehicles": {
        "name": "Vehicles",
        "description": "Vehicle tracking, rego/insurance reminders, and maintenance scheduling",

        "icon": "car",
    },
    "selfcare": {
        "name": "Self Care",
        "description": "Medication tracking and personal care reminders with ADHD-friendly 'last done' tracking",

        "icon": "heart-pulse",
    },
}


@router.get("/api/modules")
def get_modules(tenant: TenantContext = Depends(get_current_user)):
    """Get all available modules with their metadata."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT module_key, enabled FROM household_modules WHERE household_id = ?",
                (household_id,)
            ).fetchall()
            enabled_map = {r["module_key"]: r["enabled"] == 1 for r in rows}

            # Get household segment and tier
            h_row = conn.execute(
                "SELECT segment, tier FROM households WHERE id = ?",
                (household_id,)
            ).fetchone()
            segment = h_row["segment"] if h_row and h_row["segment"] else "household"
            tier = h_row["tier"] if h_row and h_row["tier"] else "free"

        beta_ids = _get_beta_household_ids()
        is_beta_household = household_id in beta_ids
        beta_keys = _get_beta_module_keys()

        modules = []
        for key, meta in AVAILABLE_MODULES.items():
            is_beta = key in beta_keys
            if is_beta and not is_beta_household:
                continue
            tier_mod = TIER_MODULES.get(key, {})
            # Resolve tier and segment for display, supporting segment_tiers
            if "segment_tiers" in tier_mod:
                display_tier = tier_mod["segment_tiers"].get(segment, list(tier_mod["segment_tiers"].values())[0])
                display_segment = list(tier_mod["segment_tiers"].keys())
            else:
                display_tier = tier_mod.get("tier", "core")
                display_segment = tier_mod.get("segment", "all")
            modules.append({
                "key": key,
                "name": meta["name"],
                "description": meta["description"],
                "tier": display_tier,
                "segment": display_segment,
                "icon": meta["icon"],
                "enabled": enabled_map.get(key, True),
                "available": is_module_available(key, segment, tier),
                "beta": is_beta,
            })

        return {
            "modules": modules,
            "household_segment": segment,
            "household_tier": tier,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")



@router.put("/api/modules/bulk-update")
async def bulk_update_modules(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Set which modules are enabled (replaces all current settings)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        enabled_keys = data.get("enabled", [])
        if not isinstance(enabled_keys, list):
            logger.warning("Validation failed in bulk_update_modules: %s", "enabled must be a list")
            raise HTTPException(status_code=400, detail="enabled must be a list of module keys")
        for k in enabled_keys:
            if not isinstance(k, str):
                logger.warning("Validation failed in bulk_update_modules: %s", "module keys must be strings")
                raise HTTPException(status_code=400, detail="module keys must be strings")
        # --- End validation ---

        all_keys = list(AVAILABLE_MODULES.keys())

        with get_db() as conn:
            for key in all_keys:
                should_enable = 1 if key in enabled_keys else 0
                conn.execute("""
                    INSERT INTO household_modules (household_id, module_key, enabled)
                    VALUES (?, ?, ?)
                    ON CONFLICT(household_id, module_key) DO UPDATE SET enabled = ?
                """, (household_id, key, should_enable, should_enable))
            conn.commit()

        await manager.broadcast({"type": "modules_updated"}, household_id=household_id)
        return {"status": "ok", "enabled": [k for k in all_keys if k in enabled_keys]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in bulk_update_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in bulk_update_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@router.put("/api/modules/{module_key}")
async def toggle_module(module_key: str, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Enable or disable a module for the household."""
    household_id = tenant.household_id
    try:
        if module_key not in AVAILABLE_MODULES:
            raise HTTPException(status_code=404, detail=f"Unknown module: {module_key}")

        data = await request.json()

        # --- Input validation ---
        if "enabled" in data and not isinstance(data["enabled"], bool):
            logger.warning("Validation failed in toggle_module: %s", "enabled must be a boolean")
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        # --- End validation ---

        enabled = 1 if data.get("enabled", True) else 0

        with get_db() as conn:
            # Log warning if enabling a module outside the household's segment/tier
            if enabled:
                h_row = conn.execute(
                    "SELECT segment, tier FROM households WHERE id = ?",
                    (household_id,)
                ).fetchone()
                segment = h_row["segment"] if h_row and h_row["segment"] else "household"
                h_tier = h_row["tier"] if h_row and h_row["tier"] else "free"
                if not is_module_available(module_key, segment, h_tier):
                    logger.warning("Household %d (%s/%s) enabled %s",
                                   household_id, segment, h_tier, module_key)

            conn.execute("""
                INSERT INTO household_modules (household_id, module_key, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(household_id, module_key) DO UPDATE SET enabled = ?
            """, (household_id, module_key, enabled, enabled))
            conn.commit()

        await manager.broadcast({"type": "modules_updated"}, household_id=household_id)
        return {"module": module_key, "enabled": enabled == 1}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in toggle_module: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in toggle_module: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")



@router.get("/api/modules/enabled")
def get_enabled_modules(tenant: TenantContext = Depends(get_current_user)):
    """Get just the list of enabled module keys for the current household.
    For dependents, filters by their dependent_permissions."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT module_key FROM household_modules WHERE household_id = ? AND enabled = 1",
                (household_id,)
            ).fetchall()

            enabled = [r["module_key"] for r in rows]

            # If no modules configured yet, all are enabled by default
            if not enabled:
                enabled = list(AVAILABLE_MODULES.keys())

            # Filter out beta modules for non-beta households
            beta_ids = _get_beta_household_ids()
            beta_keys = _get_beta_module_keys()
            if household_id not in beta_ids:
                enabled = [k for k in enabled if k not in beta_keys]

            # For dependents, filter to only permitted modules
            if tenant.role == "dependent":
                member = conn.execute(
                    "SELECT id FROM household_members WHERE user_id = ? AND household_id = ?",
                    (tenant.user_id, household_id)
                ).fetchone()
                if member:
                    perms = conn.execute(
                        "SELECT module_key FROM dependent_permissions WHERE member_id = ? AND household_id = ? AND access_level != 'hidden'",
                        (member["id"], household_id)
                    ).fetchall()
                    allowed = {r["module_key"] for r in perms}
                    enabled = [k for k in enabled if k in allowed]

        beta_modules = [k for k in enabled if k in beta_keys]
        return {"enabled": enabled, "beta": beta_modules}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_enabled_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_enabled_modules: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/dependent-permissions/{member_id}")
def get_dependent_permissions(member_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get module permissions for a dependent. Manager only."""
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager access required")
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            # Verify the member is a dependent in this household
            member = conn.execute(
                "SELECT id, display_name, role FROM household_members WHERE id = ? AND household_id = ?",
                (member_id, household_id)
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")
            if member["role"] != "dependent":
                raise HTTPException(status_code=400, detail="Member is not a dependent")

            perms = conn.execute(
                "SELECT module_key, access_level FROM dependent_permissions WHERE member_id = ? AND household_id = ?",
                (member_id, household_id)
            ).fetchall()
            perm_map = {r["module_key"]: r["access_level"] for r in perms}

            # Build full list with defaults (hidden if not set)
            result = []
            for key, meta in AVAILABLE_MODULES.items():
                if key == "feedback":
                    continue  # Skip feedback for dependents
                result.append({
                    "module_key": key,
                    "name": meta["name"],
                    "access_level": perm_map.get(key, "hidden"),
                })

            return {
                "member_id": member_id,
                "display_name": member["display_name"],
                "permissions": result,
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_dependent_permissions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_dependent_permissions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/dependent-permissions/{member_id}")
async def set_dependent_permissions(member_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Set module permissions for a dependent. Manager only.
    Body: { "permissions": { "chores": "full", "meals": "view", "bills": "hidden" } }
    """
    if tenant.role != "manager":
        raise HTTPException(status_code=403, detail="Manager access required")
    household_id = tenant.household_id
    try:
        data = await request.json()
        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            raise HTTPException(status_code=400, detail="permissions must be a dict")

        valid_levels = {"hidden", "view", "full"}

        with get_db() as conn:
            # Verify the member is a dependent in this household
            member = conn.execute(
                "SELECT id, role FROM household_members WHERE id = ? AND household_id = ?",
                (member_id, household_id)
            ).fetchone()
            if not member:
                raise HTTPException(status_code=404, detail="Member not found")
            if member["role"] != "dependent":
                raise HTTPException(status_code=400, detail="Member is not a dependent")

            for module_key, level in permissions.items():
                if module_key not in AVAILABLE_MODULES:
                    continue
                if level not in valid_levels:
                    continue
                conn.execute("""
                    INSERT INTO dependent_permissions (household_id, member_id, module_key, access_level)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(household_id, member_id, module_key)
                    DO UPDATE SET access_level = ?
                """, (household_id, member_id, module_key, level, level))
            conn.commit()

        await manager.broadcast({"type": "modules_updated"}, household_id=household_id)
        return {"message": "Permissions updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in set_dependent_permissions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in set_dependent_permissions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/modules/summaries")
def get_module_summaries(tenant: TenantContext = Depends(get_current_user)):
    """Get summary text and status for all enabled modules (used by home screen tiles)."""
    household_id = tenant.household_id
    my_name = tenant.display_name
    try:
        tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
        today = datetime.now(tz).date()
        today_iso = today.isoformat()
        today_dow = today.weekday()  # 0=Monday

        summaries = {}

        with get_db() as conn:
            # --- Chores ---
            try:
                from routers.chores import get_chores_with_status
                chores = get_chores_with_status(conn, household_id)
                overdue = sum(1 for c in chores if c['status'] == 'overdue')
                due_today = sum(1 for c in chores if c['status'] == 'due_today')
                completed = sum(1 for c in chores if c['status'] == 'completed_today')
                parts = []
                if overdue:
                    parts.append(f"{overdue} overdue")
                if due_today:
                    parts.append(f"{due_today} due today")
                if completed:
                    parts.append(f"{completed} done")
                if not parts:
                    parts.append("All clear")
                status = "red" if overdue else ("green" if not due_today else "neutral")
                summaries["chores"] = {"summary": ", ".join(parts), "status": status, "badge": overdue or None}
            except Exception as e:
                logger.warning("Summaries: chores error: %s", e)
                summaries["chores"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Ad-hoc Tasks ---
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) as c FROM adhoc_tasks WHERE household_id = ? AND completed = 0 AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()['c']
                my_tasks = conn.execute(
                    "SELECT COUNT(*) as c FROM adhoc_tasks WHERE household_id = ? AND completed = 0 AND deleted_at IS NULL AND assigned_to = ?",
                    (household_id, my_name)
                ).fetchone()['c']
                summary = f"{pending} pending" if pending else "No tasks"
                status = "red" if my_tasks else ("neutral" if pending else "green")
                summaries["adhoc_tasks"] = {"summary": summary, "status": status, "badge": my_tasks or None}
            except Exception as e:
                logger.warning("Summaries: adhoc error: %s", e)
                summaries["adhoc_tasks"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Calendar ---
            try:
                cal_count = conn.execute(
                    "SELECT COUNT(*) as c FROM calendar_events WHERE household_id = ? AND deleted_at IS NULL AND start_date = ?",
                    (household_id, today_iso)
                ).fetchone()['c']
                summary = f"{cal_count} event{'s' if cal_count != 1 else ''} today" if cal_count else "No events today"
                summaries["calendar"] = {"summary": summary, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: calendar error: %s", e)
                summaries["calendar"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Meals ---
            try:
                dinners = conn.execute(
                    "SELECT meal_name FROM meals WHERE household_id = ? AND deleted_at IS NULL AND day_of_week = ? AND meal_type = 'dinner'",
                    (household_id, today_dow)
                ).fetchall()
                if dinners:
                    names = list(set(r['meal_name'] for r in dinners))
                    summary = names[0] if len(names) == 1 else f"{len(names)} options tonight"
                    status = "neutral"
                else:
                    summary = "No dinner planned"
                    status = "orange"
                # Add cook info to summary
                try:
                    from routers.meals import get_duties_for_household
                    tz_name = get_setting("timezone", "Australia/Sydney", household_id)
                    duties = get_duties_for_household(conn, household_id, tz_name)
                    tonight_cook = duties.get("cook_dinner", {}).get(today_dow)
                    if tonight_cook:
                        if tonight_cook == my_name:
                            summary += " · You're cooking"
                        else:
                            summary += f" · {tonight_cook}'s cooking"
                except Exception:
                    pass
                summaries["meals"] = {"summary": summary, "status": status, "badge": None}
            except Exception as e:
                logger.warning("Summaries: meals error: %s", e)
                summaries["meals"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Bills ---
            try:
                week_end = (today + timedelta(days=7)).isoformat()
                overdue_bills = conn.execute(
                    "SELECT COUNT(*) as c FROM bills WHERE household_id = ? AND deleted_at IS NULL AND paid = 0 AND due_date < ?",
                    (household_id, today_iso)
                ).fetchone()['c']
                due_soon = conn.execute(
                    "SELECT SUM(COALESCE(amount, 0)) as total FROM bills WHERE household_id = ? AND deleted_at IS NULL AND paid = 0 AND due_date <= ?",
                    (household_id, week_end)
                ).fetchone()['total'] or 0
                if overdue_bills:
                    summary = f"{overdue_bills} overdue"
                    status = "red"
                elif due_soon > 0:
                    summary = f"${due_soon:,.0f} due this week"
                    status = "neutral"
                else:
                    summary = "All paid up"
                    status = "green"
                summaries["bills"] = {"summary": summary, "status": status, "badge": overdue_bills or None}
            except Exception as e:
                logger.warning("Summaries: bills error: %s", e)
                summaries["bills"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Shopping ---
            try:
                to_buy = conn.execute(
                    "SELECT COUNT(*) as c FROM shopping_items WHERE household_id = ? AND deleted_at IS NULL AND purchased = 0",
                    (household_id,)
                ).fetchone()['c']
                summary = f"{to_buy} item{'s' if to_buy != 1 else ''} to buy" if to_buy else "List empty"
                summaries["shopping"] = {"summary": summary, "status": "neutral", "badge": to_buy or None}
            except Exception as e:
                logger.warning("Summaries: shopping error: %s", e)
                summaries["shopping"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Inventory ---
            try:
                low = conn.execute(
                    "SELECT COUNT(*) as c FROM inventory_items WHERE household_id = ? AND deleted_at IS NULL AND quantity <= low_threshold",
                    (household_id,)
                ).fetchone()['c']
                summary = f"{low} item{'s' if low != 1 else ''} low" if low else "Stock OK"
                status = "orange" if low else "green"
                summaries["inventory"] = {"summary": summary, "status": status, "badge": low or None}
            except Exception as e:
                logger.warning("Summaries: inventory error: %s", e)
                summaries["inventory"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Polls ---
            try:
                active = conn.execute(
                    "SELECT COUNT(*) as c FROM polls WHERE household_id = ? AND deleted_at IS NULL AND status = 'active'",
                    (household_id,)
                ).fetchone()['c']
                summary = f"{active} active poll{'s' if active != 1 else ''}" if active else "No active polls"
                status = "blue" if active else "neutral"
                summaries["polls"] = {"summary": summary, "status": status, "badge": active or None}
            except Exception as e:
                logger.warning("Summaries: polls error: %s", e)
                summaries["polls"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Recipes ---
            try:
                count = conn.execute(
                    "SELECT COUNT(*) as c FROM recipes WHERE household_id = ?",
                    (household_id,)
                ).fetchone()['c']
                summary = f"{count} recipe{'s' if count != 1 else ''}" if count else "No recipes yet"
                summaries["recipes"] = {"summary": summary, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: recipes error: %s", e)
                summaries["recipes"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Routines ---
            try:
                routines_list = conn.execute(
                    "SELECT id, assigned_to FROM routines WHERE household_id = ?",
                    (household_id,)
                ).fetchall()
                if routines_list:
                    all_done_count = 0
                    for rt in routines_list:
                        item_count = conn.execute(
                            "SELECT COUNT(*) as c FROM routine_items WHERE routine_id = ?",
                            (rt["id"],)
                        ).fetchone()["c"]
                        done_count = conn.execute(
                            "SELECT COUNT(DISTINCT item_id) as c FROM routine_completions WHERE routine_id = ? AND completed_date = ?",
                            (rt["id"], today_iso)
                        ).fetchone()["c"]
                        if item_count > 0 and done_count >= item_count:
                            all_done_count += 1
                    pending = len(routines_list) - all_done_count
                    if pending == 0:
                        summary = "All done!"
                        status = "green"
                    else:
                        summary = f"{pending} routine{'s' if pending != 1 else ''} to go"
                        status = "neutral"
                    summaries["routines"] = {"summary": summary, "status": status, "badge": pending or None}
                else:
                    summaries["routines"] = {"summary": "No routines", "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: routines error: %s", e)
                summaries["routines"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Rewards ---
            try:
                pending_redemptions = conn.execute(
                    "SELECT COUNT(*) as c FROM reward_redemptions WHERE household_id = ? AND status = 'pending'",
                    (household_id,)
                ).fetchone()["c"]
                total_stars = conn.execute(
                    "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE household_id = ?",
                    (household_id,)
                ).fetchone()["total"]
                if pending_redemptions:
                    summary = f"{pending_redemptions} pending approval"
                    status = "orange"
                elif total_stars > 0:
                    summary = f"{total_stars} stars earned"
                    status = "neutral"
                else:
                    summary = "No stars yet"
                    status = "neutral"
                summaries["rewards"] = {"summary": summary, "status": status, "badge": pending_redemptions or None}
            except Exception as e:
                logger.warning("Summaries: rewards error: %s", e)
                summaries["rewards"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Allowances ---
            try:
                active_allowances = conn.execute(
                    "SELECT COUNT(*) as c FROM allowance_settings WHERE household_id = ? AND active = 1",
                    (household_id,)
                ).fetchone()["c"]
                active_goals = conn.execute(
                    "SELECT COUNT(*) as c FROM savings_goals WHERE household_id = ? AND deleted_at IS NULL AND completed = 0",
                    (household_id,)
                ).fetchone()["c"]
                if active_goals:
                    summary = f"{active_goals} savings goal{'s' if active_goals != 1 else ''}"
                elif active_allowances:
                    summary = f"{active_allowances} allowance{'s' if active_allowances != 1 else ''}"
                else:
                    summary = "Not set up"
                summaries["allowances"] = {"summary": summary, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: allowances error: %s", e)
                summaries["allowances"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Pets ---
            try:
                pet_count = conn.execute(
                    "SELECT COUNT(*) as c FROM pets WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()["c"]
                if pet_count:
                    from datetime import date as _date
                    today_iso = _date.today().isoformat()
                    upcoming_vet = conn.execute(
                        "SELECT COUNT(*) as c FROM pet_vet_appointments WHERE household_id = ? AND date >= ? AND completed = 0",
                        (household_id, today_iso)
                    ).fetchone()["c"]
                    if upcoming_vet:
                        summary = f"{pet_count} pet{'s' if pet_count != 1 else ''}, {upcoming_vet} appt{'s' if upcoming_vet != 1 else ''}"
                    else:
                        summary = f"{pet_count} pet{'s' if pet_count != 1 else ''}"
                else:
                    summary = "No pets"
                summaries["pets"] = {"summary": summary, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: pets error: %s", e)
                summaries["pets"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Noticeboard ---
            try:
                now_iso = datetime.now().isoformat()
                active_posts = conn.execute(
                    "SELECT COUNT(*) as c FROM noticeboard_posts WHERE household_id = ? AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
                    (household_id, now_iso)
                ).fetchone()["c"]
                pinned = conn.execute(
                    "SELECT COUNT(*) as c FROM noticeboard_posts WHERE household_id = ? AND pinned = 1 AND (expires_at IS NULL OR expires_at > ?)",
                    (household_id, now_iso)
                ).fetchone()["c"]
                if pinned:
                    summary = f"{pinned} pinned, {active_posts} total"
                elif active_posts:
                    summary = f"{active_posts} post{'s' if active_posts != 1 else ''}"
                else:
                    summary = "No notices"
                summaries["noticeboard"] = {"summary": summary, "status": "neutral", "badge": active_posts or None}
            except Exception as e:
                logger.warning("Summaries: noticeboard error: %s", e)
                summaries["noticeboard"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Assignments ---
            try:
                pending_assignments = conn.execute(
                    "SELECT COUNT(*) as c FROM assignments WHERE household_id = ? AND deleted_at IS NULL AND status != 'done'",
                    (household_id,)
                ).fetchone()["c"]
                overdue_assignments = conn.execute(
                    "SELECT COUNT(*) as c FROM assignments WHERE household_id = ? AND deleted_at IS NULL AND status != 'done' AND due_date < ?",
                    (household_id, today_iso)
                ).fetchone()["c"]
                if overdue_assignments:
                    summary = f"{overdue_assignments} overdue, {pending_assignments} total"
                    status = "red"
                elif pending_assignments:
                    summary = f"{pending_assignments} pending"
                    status = "neutral"
                else:
                    summary = "All done"
                    status = "green"
                summaries["assignments"] = {"summary": summary, "status": status, "badge": overdue_assignments or None}
            except Exception as e:
                logger.warning("Summaries: assignments error: %s", e)
                summaries["assignments"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Feedback ---
            try:
                new_feedback = conn.execute(
                    "SELECT COUNT(*) as c FROM feedback WHERE household_id = ? AND status = 'new'",
                    (household_id,)
                ).fetchone()["c"]
                if new_feedback:
                    summary = f"{new_feedback} new item{'s' if new_feedback != 1 else ''}"
                else:
                    summary = "All clear"
                summaries["feedback"] = {"summary": summary, "status": "neutral", "badge": new_feedback or None}
            except Exception as e:
                logger.warning("Summaries: feedback error: %s", e)
                summaries["feedback"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Expenses ---
            try:
                unsettled = conn.execute(
                    "SELECT COUNT(*) as c FROM expenses WHERE household_id = ?",
                    (household_id,)
                ).fetchone()["c"]
                if unsettled:
                    total = conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE household_id = ?",
                        (household_id,)
                    ).fetchone()["total"]
                    summary = f"${total:,.0f} tracked"
                else:
                    summary = "No expenses"
                summaries["expenses"] = {"summary": summary, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Summaries: expenses error: %s", e)
                summaries["expenses"] = {"summary": "", "status": "neutral", "badge": None}

            # --- House Rules ---
            try:
                rule_count = conn.execute(
                    "SELECT COUNT(*) as c FROM house_rules WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()["c"]
                if rule_count:
                    unacked = conn.execute("""
                        SELECT COUNT(DISTINCT hr.id) as c FROM house_rules hr
                        WHERE hr.household_id = ? AND hr.deleted_at IS NULL AND hr.id NOT IN (
                            SELECT DISTINCT rule_id FROM house_rule_acknowledgements
                            WHERE household_id = ? AND person = ?
                        )
                    """, (household_id, household_id, my_name)).fetchone()["c"]
                    if unacked:
                        summary = f"{unacked} need{'s' if unacked == 1 else ''} acknowledgement"
                        status = "orange"
                    else:
                        summary = f"{rule_count} rule{'s' if rule_count != 1 else ''}, all acknowledged"
                        status = "green"
                else:
                    summary = "No rules set"
                    status = "neutral"
                summaries["house_rules"] = {"summary": summary, "status": status, "badge": unacked if rule_count and unacked else None}
            except Exception as e:
                logger.warning("Summaries: house_rules error: %s", e)
                summaries["house_rules"] = {"summary": "", "status": "neutral", "badge": None}

            # --- Tenancy ---
            try:
                tenancy_row = conn.execute(
                    "SELECT lease_end FROM tenancy WHERE household_id = ?",
                    (household_id,)
                ).fetchone()
                if tenancy_row and tenancy_row["lease_end"]:
                    from datetime import date
                    lease_end = date.fromisoformat(tenancy_row["lease_end"])
                    days_left = (lease_end - today).days
                    if days_left < 0:
                        summary = f"Lease expired {abs(days_left)} day{'s' if abs(days_left) != 1 else ''} ago"
                        status = "red"
                    elif days_left <= 30:
                        summary = f"{days_left} day{'s' if days_left != 1 else ''} remaining"
                        status = "orange"
                    else:
                        summary = f"{days_left} days remaining"
                        status = "neutral"
                elif tenancy_row:
                    summary = "Lease details saved"
                    status = "neutral"
                else:
                    summary = "Not set up"
                    status = "neutral"
                summaries["tenancy"] = {"summary": summary, "status": status, "badge": None}
            except Exception as e:
                logger.warning("Summaries: tenancy error: %s", e)
                summaries["tenancy"] = {"summary": "", "status": "neutral", "badge": None}

        # --- Vehicles ---
        try:
            vehicle_count = conn.execute(
                "SELECT COUNT(*) as c FROM vehicles WHERE household_id = ?",
                (household_id,)
            ).fetchone()["c"]
            if vehicle_count:
                upcoming_maint = conn.execute(
                    "SELECT COUNT(*) as c FROM vehicle_maintenance WHERE household_id = ? AND completed = 0",
                    (household_id,)
                ).fetchone()["c"]
                if upcoming_maint:
                    summary = f"{vehicle_count} vehicle{'s' if vehicle_count != 1 else ''}, {upcoming_maint} maintenance due"
                    status = "orange"
                else:
                    summary = f"{vehicle_count} vehicle{'s' if vehicle_count != 1 else ''}"
                    status = "neutral"
            else:
                summary = "No vehicles"
                status = "neutral"
            summaries["vehicles"] = {"summary": summary, "status": status, "badge": upcoming_maint if vehicle_count and upcoming_maint else None}
        except Exception as e:
            logger.warning("Summaries: vehicles error: %s", e)
            summaries["vehicles"] = {"summary": "", "status": "neutral", "badge": None}

        # --- Self Care (per-category summaries) ---
        try:
            tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
            sc_today = datetime.now(tz).date()
            all_sc_items = conn.execute(
                "SELECT id, frequency_days, category, box_quantity, box_total, repeats_remaining, dose_quantity FROM selfcare_items WHERE household_id = ?",
                (household_id,)
            ).fetchall()

            total_due = 0
            total_stock_alerts = 0
            for cat_key in ("medication", "personal_care", "life_admin"):
                cat_items = [it for it in all_sc_items if (it["category"] or "medication") == cat_key]
                due_count = 0
                stock_alert_count = 0
                for it in cat_items:
                    if it["frequency_days"] > 0:
                        last_log = conn.execute(
                            "SELECT logged_at FROM selfcare_logs WHERE item_id = ? AND household_id = ? ORDER BY logged_at DESC LIMIT 1",
                            (it["id"], household_id)
                        ).fetchone()
                        if not last_log:
                            due_count += 1
                        else:
                            last_date = datetime.fromisoformat(last_log["logged_at"]).date()
                            if (sc_today - last_date).days >= it["frequency_days"]:
                                due_count += 1
                    # Check stock alerts for medication
                    bq = it["box_quantity"]
                    if bq is not None and cat_key == "medication":
                        if bq <= 0:
                            stock_alert_count += 1
                        elif it["frequency_days"] > 0 and (it["dose_quantity"] or 1) > 0:
                            days_left = (bq / (it["dose_quantity"] or 1)) * it["frequency_days"]
                            if days_left <= 7:
                                stock_alert_count += 1
                        rr = it["repeats_remaining"]
                        if rr is not None and rr <= 1:
                            stock_alert_count += 1
                badge_count = due_count + stock_alert_count
                if cat_items:
                    parts = []
                    if due_count:
                        parts.append(f"{due_count} due")
                    if stock_alert_count:
                        parts.append(f"{stock_alert_count} alert{'s' if stock_alert_count != 1 else ''}")
                    if parts:
                        summary = ", ".join(parts)
                        status = "red" if stock_alert_count else "orange"
                    else:
                        summary = f"{len(cat_items)} tracked"
                        status = "green"
                else:
                    summary = "No items"
                    status = "neutral"
                    badge_count = 0
                summaries[f"selfcare_{cat_key}"] = {"summary": summary, "status": status, "badge": badge_count if badge_count else None}
                total_due += due_count
                total_stock_alerts += stock_alert_count

            item_count = len(all_sc_items)
            total_badge = total_due + total_stock_alerts
            if item_count:
                parts = []
                if total_due:
                    parts.append(f"{total_due} due")
                if total_stock_alerts:
                    parts.append(f"{total_stock_alerts} stock alert{'s' if total_stock_alerts != 1 else ''}")
                if parts:
                    summary = ", ".join(parts)
                    status = "red" if total_stock_alerts else "orange"
                else:
                    summary = f"{item_count} item{'s' if item_count != 1 else ''}, all up to date"
                    status = "green"
            else:
                summary = "No items"
                status = "neutral"
            summaries["selfcare"] = {"summary": summary, "status": status, "badge": total_badge if total_badge else None}
        except Exception as e:
            logger.warning("Summaries: selfcare error: %s", e)
            summaries["selfcare"] = {"summary": "", "status": "neutral", "badge": None}
            for cat_key in ("medication", "personal_care", "life_admin"):
                summaries[f"selfcare_{cat_key}"] = {"summary": "", "status": "neutral", "badge": None}

        # --- Fuel (external API, use cache) ---
        try:
            from fuel import fuel_cache, parse_fuel_cache_file
            fuel_data = fuel_cache.get("data")
            if not fuel_data:
                fuel_data = parse_fuel_cache_file()
            if fuel_data and fuel_data.get("fuel", {}).get("E10"):
                cheapest = fuel_data["fuel"]["E10"][0]["price"]
                summary = f"E10: {cheapest:.1f}c/L"
            else:
                summary = "No data"
            summaries["fuel"] = {"summary": summary, "status": "neutral", "badge": None}
        except Exception as e:
            logger.warning("Summaries: fuel error: %s", e)
            summaries["fuel"] = {"summary": "Unavailable", "status": "neutral", "badge": None}

        # --- Weather (external API) ---
        try:
            import urllib.request
            lat = float(get_setting("location_lat", "-32.879", household_id))
            lon = float(get_setting("location_lon", "151.691", household_id))
            tz_str = get_setting("timezone", "Australia/Sydney", household_id)
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,weather_code"
                f"&timezone={tz_str.replace('/', '%2F')}"
            )
            weather_codes = {
                0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Cloudy",
                45: "Foggy", 48: "Fog", 51: "Light Drizzle", 53: "Drizzle",
                55: "Heavy Drizzle", 61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
                80: "Showers", 81: "Showers", 82: "Heavy Showers", 95: "Thunderstorm",
            }
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode())
                temp = data["current"]["temperature_2m"]
                code = data["current"]["weather_code"]
                desc = weather_codes.get(code, "")
                summary = f"{temp:.0f}\u00b0C, {desc}" if desc else f"{temp:.0f}\u00b0C"
            summaries["weather"] = {"summary": summary, "status": "neutral", "badge": None}
        except Exception as e:
            logger.warning("Summaries: weather error: %s", e)
            summaries["weather"] = {"summary": "Unavailable", "status": "neutral", "badge": None}

        return {"summaries": summaries}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_module_summaries: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_module_summaries: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/home/summary")
def home_summary(tenant: TenantContext = Depends(get_current_user)):
    """Fast, DB-only summary counts for home screen tiles. No external API calls."""
    household_id = tenant.household_id
    my_name = tenant.display_name
    try:
        tz = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
        today = datetime.now(tz).date()
        today_iso = today.isoformat()
        today_dow = today.weekday()  # 0=Monday

        result = {}

        with get_db() as conn:
            # --- Chores ---
            try:
                from routers.chores import get_chores_with_status
                chores = get_chores_with_status(conn, household_id)
                overdue = sum(1 for c in chores if c['status'] == 'overdue')
                due_today = sum(1 for c in chores if c['status'] == 'due_today')
                completed = sum(1 for c in chores if c['status'] == 'completed_today')
                parts = []
                if overdue:
                    parts.append(f"{overdue} overdue")
                if due_today:
                    parts.append(f"{due_today} due")
                if completed:
                    parts.append(f"{completed} done")
                text = ", ".join(parts) if parts else "All clear"
                status = "red" if overdue else ("green" if not due_today else "neutral")
                result["chores"] = {"text": text, "status": status, "badge": overdue or None}
            except Exception as e:
                logger.warning("Home summary: chores error: %s", e)
                result["chores"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Ad-hoc Tasks ---
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) as c FROM adhoc_tasks WHERE household_id = ? AND completed = 0 AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()['c']
                my_tasks = conn.execute(
                    "SELECT COUNT(*) as c FROM adhoc_tasks WHERE household_id = ? AND completed = 0 AND deleted_at IS NULL AND assigned_to = ?",
                    (household_id, my_name)
                ).fetchone()['c']
                text = f"{pending} pending" if pending else "All clear"
                status = "red" if my_tasks else ("neutral" if pending else "green")
                result["adhoc_tasks"] = {"text": text, "status": status, "badge": my_tasks or None}
            except Exception as e:
                logger.warning("Home summary: adhoc error: %s", e)
                result["adhoc_tasks"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Calendar ---
            try:
                cal_count = conn.execute(
                    "SELECT COUNT(*) as c FROM calendar_events WHERE household_id = ? AND deleted_at IS NULL AND start_date = ?",
                    (household_id, today_iso)
                ).fetchone()['c']
                text = f"{cal_count} event{'s' if cal_count != 1 else ''} today" if cal_count else "Nothing today"
                result["calendar"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: calendar error: %s", e)
                result["calendar"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Meals ---
            try:
                dinners = conn.execute(
                    "SELECT meal_name FROM meals WHERE household_id = ? AND deleted_at IS NULL AND day_of_week = ? AND meal_type = 'dinner'",
                    (household_id, today_dow)
                ).fetchall()
                if dinners:
                    names = list(set(r['meal_name'] for r in dinners))
                    text = names[0] if len(names) == 1 else f"{len(names)} options tonight"
                else:
                    text = "No dinner planned"
                result["meals"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: meals error: %s", e)
                result["meals"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Bills ---
            try:
                week_end = (today + timedelta(days=7)).isoformat()
                overdue_bills = conn.execute(
                    "SELECT COUNT(*) as c FROM bills WHERE household_id = ? AND deleted_at IS NULL AND paid = 0 AND due_date < ?",
                    (household_id, today_iso)
                ).fetchone()['c']
                due_soon = conn.execute(
                    "SELECT SUM(COALESCE(amount, 0)) as total FROM bills WHERE household_id = ? AND deleted_at IS NULL AND paid = 0 AND due_date <= ?",
                    (household_id, week_end)
                ).fetchone()['total'] or 0
                if overdue_bills:
                    text = f"{overdue_bills} overdue"
                    status = "red"
                elif due_soon > 0:
                    text = f"${due_soon:,.0f} due this week"
                    status = "neutral"
                else:
                    text = "All paid up"
                    status = "green"
                result["bills"] = {"text": text, "status": status, "badge": overdue_bills or None}
            except Exception as e:
                logger.warning("Home summary: bills error: %s", e)
                result["bills"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Shopping ---
            try:
                to_buy = conn.execute(
                    "SELECT COUNT(*) as c FROM shopping_items WHERE household_id = ? AND deleted_at IS NULL AND purchased = 0",
                    (household_id,)
                ).fetchone()['c']
                text = f"{to_buy} item{'s' if to_buy != 1 else ''} to buy" if to_buy else "List empty"
                result["shopping"] = {"text": text, "status": "neutral", "badge": to_buy or None}
            except Exception as e:
                logger.warning("Home summary: shopping error: %s", e)
                result["shopping"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Inventory ---
            try:
                low = conn.execute(
                    "SELECT COUNT(*) as c FROM inventory_items WHERE household_id = ? AND deleted_at IS NULL AND quantity <= low_threshold",
                    (household_id,)
                ).fetchone()['c']
                text = f"{low} item{'s' if low != 1 else ''} low" if low else "Stock OK"
                status = "orange" if low else "green"
                result["inventory"] = {"text": text, "status": status, "badge": low or None}
            except Exception as e:
                logger.warning("Home summary: inventory error: %s", e)
                result["inventory"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Polls ---
            try:
                active = conn.execute(
                    "SELECT COUNT(*) as c FROM polls WHERE household_id = ? AND deleted_at IS NULL AND status = 'active'",
                    (household_id,)
                ).fetchone()['c']
                text = f"{active} active poll{'s' if active != 1 else ''}" if active else "No active polls"
                status = "blue" if active else "neutral"
                result["polls"] = {"text": text, "status": status, "badge": active or None}
            except Exception as e:
                logger.warning("Home summary: polls error: %s", e)
                result["polls"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Recipes ---
            try:
                count = conn.execute(
                    "SELECT COUNT(*) as c FROM recipes WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()['c']
                text = f"{count} recipe{'s' if count != 1 else ''}" if count else "No recipes yet"
                result["recipes"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: recipes error: %s", e)
                result["recipes"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Routines ---
            try:
                routines_list = conn.execute(
                    "SELECT id FROM routines WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchall()
                if routines_list:
                    all_done_count = 0
                    for rt in routines_list:
                        item_count = conn.execute(
                            "SELECT COUNT(*) as c FROM routine_items WHERE routine_id = ?",
                            (rt["id"],)
                        ).fetchone()["c"]
                        done_count = conn.execute(
                            "SELECT COUNT(DISTINCT item_id) as c FROM routine_completions WHERE routine_id = ? AND completed_date = ?",
                            (rt["id"], today_iso)
                        ).fetchone()["c"]
                        if item_count > 0 and done_count >= item_count:
                            all_done_count += 1
                    pending = len(routines_list) - all_done_count
                    if pending == 0:
                        text = "All done!"
                        status = "green"
                    else:
                        text = f"{pending} routine{'s' if pending != 1 else ''} to go"
                        status = "neutral"
                    result["routines"] = {"text": text, "status": status, "badge": pending or None}
                else:
                    result["routines"] = {"text": "No routines", "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: routines error: %s", e)
                result["routines"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Rewards ---
            try:
                pending_redemptions = conn.execute(
                    "SELECT COUNT(*) as c FROM reward_redemptions WHERE household_id = ? AND status = 'pending'",
                    (household_id,)
                ).fetchone()["c"]
                total_stars = conn.execute(
                    "SELECT COALESCE(SUM(points), 0) as total FROM reward_points WHERE household_id = ?",
                    (household_id,)
                ).fetchone()["total"]
                if pending_redemptions:
                    text = f"{pending_redemptions} pending"
                    status = "orange"
                elif total_stars > 0:
                    text = f"{total_stars} stars"
                    status = "neutral"
                else:
                    text = "No stars yet"
                    status = "neutral"
                result["rewards"] = {"text": text, "status": status, "badge": pending_redemptions or None}
            except Exception as e:
                logger.warning("Home summary: rewards error: %s", e)
                result["rewards"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Allowances ---
            try:
                active_allowances = conn.execute(
                    "SELECT COUNT(*) as c FROM allowance_settings WHERE household_id = ? AND active = 1",
                    (household_id,)
                ).fetchone()["c"]
                active_goals = conn.execute(
                    "SELECT COUNT(*) as c FROM savings_goals WHERE household_id = ? AND deleted_at IS NULL AND completed = 0",
                    (household_id,)
                ).fetchone()["c"]
                if active_goals:
                    text = f"{active_goals} goal{'s' if active_goals != 1 else ''}"
                elif active_allowances:
                    text = f"{active_allowances} set up"
                else:
                    text = "Not set up"
                result["allowances"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: allowances error: %s", e)
                result["allowances"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Pets ---
            try:
                pet_count = conn.execute(
                    "SELECT COUNT(*) as c FROM pets WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()["c"]
                if pet_count:
                    upcoming_vet = conn.execute(
                        "SELECT COUNT(*) as c FROM pet_vet_appointments WHERE household_id = ? AND date >= ? AND completed = 0",
                        (household_id, today_iso)
                    ).fetchone()["c"]
                    if upcoming_vet:
                        text = f"{pet_count} pet{'s' if pet_count != 1 else ''}, {upcoming_vet} appt{'s' if upcoming_vet != 1 else ''}"
                    else:
                        text = f"{pet_count} pet{'s' if pet_count != 1 else ''}"
                else:
                    text = "No pets"
                result["pets"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: pets error: %s", e)
                result["pets"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Noticeboard ---
            try:
                now_iso = datetime.now().isoformat()
                active_posts = conn.execute(
                    "SELECT COUNT(*) as c FROM noticeboard_posts WHERE household_id = ? AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
                    (household_id, now_iso)
                ).fetchone()["c"]
                if active_posts:
                    text = f"{active_posts} post{'s' if active_posts != 1 else ''}"
                else:
                    text = "No notices"
                result["noticeboard"] = {"text": text, "status": "neutral", "badge": active_posts or None}
            except Exception as e:
                logger.warning("Home summary: noticeboard error: %s", e)
                result["noticeboard"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Assignments ---
            try:
                pending_assignments = conn.execute(
                    "SELECT COUNT(*) as c FROM assignments WHERE household_id = ? AND deleted_at IS NULL AND status != 'done'",
                    (household_id,)
                ).fetchone()["c"]
                overdue_assignments = conn.execute(
                    "SELECT COUNT(*) as c FROM assignments WHERE household_id = ? AND deleted_at IS NULL AND status != 'done' AND due_date < ?",
                    (household_id, today_iso)
                ).fetchone()["c"]
                if overdue_assignments:
                    text = f"{overdue_assignments} overdue"
                    status = "red"
                elif pending_assignments:
                    text = f"{pending_assignments} pending"
                    status = "neutral"
                else:
                    text = "All done"
                    status = "green"
                result["assignments"] = {"text": text, "status": status, "badge": overdue_assignments or None}
            except Exception as e:
                logger.warning("Home summary: assignments error: %s", e)
                result["assignments"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Feedback ---
            try:
                new_feedback = conn.execute(
                    "SELECT COUNT(*) as c FROM feedback WHERE household_id = ? AND status = 'new'",
                    (household_id,)
                ).fetchone()["c"]
                if new_feedback:
                    text = f"{new_feedback} new item{'s' if new_feedback != 1 else ''}"
                else:
                    text = "All clear"
                result["feedback"] = {"text": text, "status": "neutral", "badge": new_feedback or None}
            except Exception as e:
                logger.warning("Home summary: feedback error: %s", e)
                result["feedback"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Expenses ---
            try:
                expense_count = conn.execute(
                    "SELECT COUNT(*) as c FROM expenses WHERE household_id = ?",
                    (household_id,)
                ).fetchone()["c"]
                if expense_count:
                    total = conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE household_id = ?",
                        (household_id,)
                    ).fetchone()["total"]
                    text = f"${total:,.0f} tracked"
                else:
                    text = "No expenses"
                result["expenses"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: expenses error: %s", e)
                result["expenses"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- House Rules ---
            try:
                rule_count = conn.execute(
                    "SELECT COUNT(*) as c FROM house_rules WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchone()["c"]
                if rule_count:
                    unacked = conn.execute("""
                        SELECT COUNT(DISTINCT hr.id) as c FROM house_rules hr
                        WHERE hr.household_id = ? AND hr.deleted_at IS NULL AND hr.id NOT IN (
                            SELECT DISTINCT rule_id FROM house_rule_acknowledgements
                            WHERE household_id = ? AND person = ?
                        )
                    """, (household_id, household_id, my_name)).fetchone()["c"]
                    if unacked:
                        text = f"{unacked} to acknowledge"
                        status = "orange"
                    else:
                        text = "All acknowledged"
                        status = "green"
                else:
                    text = "No rules"
                    status = "neutral"
                result["house_rules"] = {"text": text, "status": status, "badge": unacked if rule_count and unacked else None}
            except Exception as e:
                logger.warning("Home summary: house_rules error: %s", e)
                result["house_rules"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Tenancy ---
            try:
                tenancy_row = conn.execute(
                    "SELECT lease_end FROM tenancy WHERE household_id = ?",
                    (household_id,)
                ).fetchone()
                if tenancy_row and tenancy_row["lease_end"]:
                    from datetime import date
                    lease_end = date.fromisoformat(tenancy_row["lease_end"])
                    days_left = (lease_end - today).days
                    if days_left < 0:
                        text = f"Lease expired"
                        status = "red"
                    elif days_left <= 30:
                        text = f"{days_left} days left"
                        status = "orange"
                    else:
                        text = f"{days_left} days left"
                        status = "neutral"
                elif tenancy_row:
                    text = "Lease saved"
                    status = "neutral"
                else:
                    text = "Not set up"
                    status = "neutral"
                result["tenancy"] = {"text": text, "status": status, "badge": None}
            except Exception as e:
                logger.warning("Home summary: tenancy error: %s", e)
                result["tenancy"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Vehicles ---
            try:
                vehicle_count = conn.execute(
                    "SELECT COUNT(*) as c FROM vehicles WHERE household_id = ?",
                    (household_id,)
                ).fetchone()["c"]
                if vehicle_count:
                    upcoming_maint = conn.execute(
                        "SELECT COUNT(*) as c FROM vehicle_maintenance WHERE household_id = ? AND completed = 0",
                        (household_id,)
                    ).fetchone()["c"]
                    if upcoming_maint:
                        text = f"{upcoming_maint} maintenance due"
                        status = "orange"
                    else:
                        text = f"{vehicle_count} vehicle{'s' if vehicle_count != 1 else ''}"
                        status = "neutral"
                else:
                    text = "No vehicles"
                    status = "neutral"
                result["vehicles"] = {"text": text, "status": status, "badge": upcoming_maint if vehicle_count and upcoming_maint else None}
            except Exception as e:
                logger.warning("Home summary: vehicles error: %s", e)
                result["vehicles"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Self Care (per-category summaries) ---
            try:
                sc_items = conn.execute(
                    "SELECT id, frequency_days, category FROM selfcare_items WHERE household_id = ? AND deleted_at IS NULL",
                    (household_id,)
                ).fetchall()
                cat_stats = {}
                for cat_key in ("medication", "personal_care", "life_admin"):
                    cat_items = [it for it in sc_items if (it["category"] or "medication") == cat_key]
                    due_count = 0
                    for it in cat_items:
                        if it["frequency_days"] <= 0:
                            continue
                        last_log = conn.execute(
                            "SELECT logged_at FROM selfcare_logs WHERE item_id = ? AND household_id = ? ORDER BY logged_at DESC LIMIT 1",
                            (it["id"], household_id)
                        ).fetchone()
                        if not last_log:
                            due_count += 1
                        else:
                            last_date = datetime.fromisoformat(last_log["logged_at"]).date()
                            if (today - last_date).days >= it["frequency_days"]:
                                due_count += 1
                    if cat_items:
                        if due_count:
                            text = f"{due_count} due"
                            status = "orange"
                        else:
                            text = f"{len(cat_items)} tracked"
                            status = "green"
                    else:
                        text = "No items"
                        status = "neutral"
                    cat_stats[cat_key] = {"text": text, "status": status, "badge": due_count if cat_items and due_count else None}

                # Individual category tiles
                result["selfcare_medication"] = cat_stats["medication"]
                result["selfcare_personal_care"] = cat_stats["personal_care"]
                result["selfcare_life_admin"] = cat_stats["life_admin"]

                # Aggregate selfcare (for backwards compat)
                total_due = sum(s["badge"] or 0 for s in cat_stats.values())
                total_items = len(sc_items)
                if total_items:
                    if total_due:
                        text = f"{total_due} item{'s' if total_due != 1 else ''} due"
                        status = "orange"
                    else:
                        text = "All up to date"
                        status = "green"
                else:
                    text = "No items"
                    status = "neutral"
                result["selfcare"] = {"text": text, "status": status, "badge": total_due or None}
            except Exception as e:
                logger.warning("Home summary: selfcare error: %s", e)
                result["selfcare"] = {"text": "\u2014", "status": "neutral", "badge": None}
                result["selfcare_medication"] = {"text": "\u2014", "status": "neutral", "badge": None}
                result["selfcare_personal_care"] = {"text": "\u2014", "status": "neutral", "badge": None}
                result["selfcare_life_admin"] = {"text": "\u2014", "status": "neutral", "badge": None}

            # --- Fuel ---
            try:
                from fuel import fuel_cache, parse_fuel_cache_file
                fuel_data = fuel_cache.get("data")
                if not fuel_data:
                    fuel_data = parse_fuel_cache_file()
                if fuel_data and fuel_data.get("fuel", {}).get("E10"):
                    cheapest = fuel_data["fuel"]["E10"][0]["price"]
                    text = f"E10: {cheapest:.1f}\u00A2/L"
                else:
                    text = "No prices"
                result["fuel"] = {"text": text, "status": "neutral", "badge": None}
            except Exception as e:
                logger.warning("Home summary: fuel error: %s", e)
                result["fuel"] = {"text": "\u2014", "status": "neutral", "badge": None}

        return result
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in home_summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in home_summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
