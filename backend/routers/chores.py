import logging
import math  # noqa: F401 — kept for potential future use
import sqlite3
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from auth import get_current_user, require_role, TenantContext
import json
import random
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from db import get_db, check_version
from settings import get_people, get_member_colors, get_setting
from websocket import manager
from push import send_push_to_person

logger = logging.getLogger("huddle")

router = APIRouter()


def _household_tz(household_id: int) -> ZoneInfo:
    """Get the ZoneInfo for a household's configured timezone."""
    tz_name = get_setting("timezone", "Australia/Sydney", household_id)
    return ZoneInfo(tz_name)


def today_local(household_id: int) -> date:
    """Get today's date in the household's local timezone."""
    return datetime.now(_household_tz(household_id)).date()


def now_local(household_id: int) -> datetime:
    """Get the current naive datetime in the household's local timezone (for storage)."""
    return datetime.now(_household_tz(household_id)).replace(tzinfo=None)


def get_last_completion(conn, chore_id: int, household_id: int) -> Optional[dict]:
    """Get the most recent completion for a chore (including skips)."""
    row = conn.execute("""
        SELECT * FROM completions
        WHERE chore_id = ? AND household_id = ?
        ORDER BY completed_at DESC
        LIMIT 1
    """, (chore_id, household_id)).fetchone()
    return dict(row) if row else None


def get_last_actual_completion(conn, chore_id: int, household_id: int) -> Optional[dict]:
    """Get the most recent actual completion (excluding skips)."""
    row = conn.execute("""
        SELECT * FROM completions
        WHERE chore_id = ? AND completed_by NOT LIKE 'SKIPPED%' AND household_id = ?
        ORDER BY completed_at DESC
        LIMIT 1
    """, (chore_id, household_id)).fetchone()
    return dict(row) if row else None


def days_since_completion(conn, chore_id: int, household_id: int) -> Optional[int]:
    """Calculate days since last completion (including skips for due date purposes)."""
    last = get_last_completion(conn, chore_id, household_id)
    if not last:
        return None
    completed_date = datetime.fromisoformat(last['completed_at']).date()
    return (today_local(household_id) - completed_date).days


def days_since_actual_completion(conn, chore_id: int, household_id: int) -> Optional[int]:
    """Calculate days since last actual completion (excluding skips)."""
    last = get_last_actual_completion(conn, chore_id, household_id)
    if not last:
        return None
    completed_date = datetime.fromisoformat(last['completed_at']).date()
    return (today_local(household_id) - completed_date).days


def is_chore_due(chore: dict, conn, household_id: int, last_completion: Optional[dict] = None) -> dict:
    """
    Determine if a chore is due and its status.
    Returns: {'status': 'overdue'|'due_today'|'tomorrow'|'not_due', 'days_overdue': int, 'due_date': date}

    If last_completion is provided, it will be used instead of querying the DB
    (allows batch-fetched data to avoid N+1 queries).
    Pass a dict for a real completion, or None to indicate no completion exists.
    Use the sentinel _UNSET to indicate the caller did not provide the parameter.
    """
    today = today_local(household_id)
    tomorrow = today + timedelta(days=1)

    start_date_str = chore.get('start_date')
    if start_date_str:
        try:
            start_date = date.fromisoformat(start_date_str)
            if start_date > today:
                if start_date == tomorrow:
                    return {'status': 'tomorrow', 'days_overdue': 0, 'due_date': start_date}
                return {'status': 'not_due', 'days_overdue': 0, 'due_date': start_date}
        except (ValueError, TypeError) as e:
            logger.warning("Invalid start_date format for chore %s: %s", chore.get('id'), e)

    # Determine the earliest date this chore could be considered due.
    # Don't count days before the chore was created or before its start_date.
    created_at_str = chore.get('created_at')
    earliest_due = None
    if created_at_str:
        try:
            earliest_due = datetime.fromisoformat(created_at_str).date()
        except (ValueError, TypeError) as e:
            logger.warning("Invalid created_at format for chore %s: %s", chore.get('id'), e)
    if start_date_str:
        try:
            sd = date.fromisoformat(start_date_str)
            if earliest_due is None or sd > earliest_due:
                earliest_due = sd
        except (ValueError, TypeError) as e:
            logger.warning("Invalid start_date format for chore %s: %s", chore.get('id'), e)

    schedule_type = chore['schedule_type']

    # Use pre-fetched last_completion if provided, otherwise query DB
    if last_completion is _UNSET:
        last_completion = get_last_completion(conn, chore['id'], household_id)

    if last_completion:
        last_date = datetime.fromisoformat(last_completion['completed_at']).date()
    else:
        last_date = None

    def check_day(check_date: date) -> bool:
        if schedule_type == 'daily':
            return True
        elif schedule_type == 'days':
            days = json.loads(chore['schedule_days'] or '[]')
            day_name = check_date.strftime('%A').lower()
            return day_name in [d.lower() for d in days]
        elif schedule_type == 'interval':
            if not last_date:
                return True
            interval = chore['schedule_interval'] or 1
            next_due = last_date + timedelta(days=interval)
            return check_date >= next_due
        return False

    completed_today = last_date == today if last_date else False

    if schedule_type == 'interval':
        interval = chore['schedule_interval'] or 1
        if last_date:
            next_due = last_date + timedelta(days=interval)
            next_next_due = next_due + timedelta(days=interval)
            tomorrow_due = (next_due == tomorrow) or (next_next_due == tomorrow)

            if completed_today:
                return {'status': 'completed_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': tomorrow_due}
            elif next_due < today:
                return {'status': 'overdue', 'days_overdue': (today - next_due).days, 'due_date': next_due, 'also_tomorrow': tomorrow_due}
            elif next_due == today:
                return {'status': 'due_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': tomorrow_due}
            elif next_due == tomorrow:
                return {'status': 'tomorrow', 'days_overdue': 0, 'due_date': tomorrow, 'also_tomorrow': True}
            else:
                return {'status': 'not_due', 'days_overdue': 0, 'due_date': next_due, 'also_tomorrow': False}
        else:
            return {'status': 'due_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': True}

    due_today = check_day(today)
    due_tomorrow = check_day(tomorrow)

    if completed_today and due_today:
        if schedule_type == 'daily':
            return {'status': 'completed_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': True}
        return {'status': 'completed_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': due_tomorrow}

    if schedule_type == 'days':
        for i in range(1, 15):
            check_date = today - timedelta(days=i)
            # Don't look before the chore existed
            if earliest_due and check_date < earliest_due:
                break
            if check_day(check_date):
                if last_date and last_date >= check_date:
                    break
                else:
                    return {'status': 'overdue', 'days_overdue': i, 'due_date': check_date, 'also_tomorrow': due_tomorrow}

    if due_today:
        return {'status': 'due_today', 'days_overdue': 0, 'due_date': today, 'also_tomorrow': due_tomorrow}
    elif due_tomorrow:
        return {'status': 'tomorrow', 'days_overdue': 0, 'due_date': tomorrow, 'also_tomorrow': True}

    return {'status': 'not_due', 'days_overdue': 0, 'due_date': None, 'also_tomorrow': False}


# Sentinel value to distinguish "caller didn't pass last_completion" from "no completion exists (None)"
_UNSET = object()


def get_chores_with_status(conn, household_id: int) -> list:
    """Get all chores with their current status, with load balancing (max 2 per person per day)."""
    chores = conn.execute("SELECT * FROM chores WHERE household_id = ? AND deleted_at IS NULL ORDER BY name", (household_id,)).fetchall()
    result = []

    # Batch-fetch all completions to avoid N+1 queries (was 5 SELECTs per chore)
    chore_ids = [dict(row)['id'] for row in chores]
    last_completion_by_chore = {}
    last_actual_completion_by_chore = {}

    if chore_ids:
        placeholders = ','.join('?' * len(chore_ids))
        all_completions = conn.execute(f"""
            SELECT chore_id, completed_at, completed_by, completed_with
            FROM completions
            WHERE chore_id IN ({placeholders}) AND household_id = ?
            ORDER BY completed_at DESC
        """, (*chore_ids, household_id)).fetchall()

        for row in all_completions:
            cid = row['chore_id']
            row_dict = dict(row)
            if cid not in last_completion_by_chore:
                last_completion_by_chore[cid] = row_dict
            if cid not in last_actual_completion_by_chore and not row['completed_by'].startswith('SKIPPED'):
                last_actual_completion_by_chore[cid] = row_dict

    today = today_local(household_id)

    for chore in chores:
        chore_dict = dict(chore)
        chore_dict['people'] = json.loads(chore_dict['people'])

        people_list = chore_dict['people']
        if people_list:
            idx = chore_dict['current_person_index'] % len(people_list)
            chore_dict['current_person'] = people_list[idx]
        else:
            chore_dict['current_person'] = None

        # Pass batch-fetched last_completion to avoid per-chore DB query inside is_chore_due
        prefetched_last = last_completion_by_chore.get(chore_dict['id'], None)
        status_info = is_chore_due(chore_dict, conn, household_id, last_completion=prefetched_last)
        chore_dict['status'] = status_info['status']
        chore_dict['days_overdue'] = status_info['days_overdue']
        chore_dict['also_tomorrow'] = status_info.get('also_tomorrow', False)

        # Use batch-fetched lookups instead of per-chore queries
        last = last_completion_by_chore.get(chore_dict['id'])
        last_actual = last_actual_completion_by_chore.get(chore_dict['id'])

        if last:
            completed_date = datetime.fromisoformat(last['completed_at']).date()
            days = (today - completed_date).days
        else:
            days = None
        chore_dict['days_since_done'] = days

        if last_actual:
            actual_date = datetime.fromisoformat(last_actual['completed_at']).date()
            days_actual = (today - actual_date).days
        else:
            days_actual = None

        if last:
            chore_dict['last_completed_by'] = last['completed_by']
            chore_dict['last_completed_at'] = last['completed_at']
            chore_dict['last_completed_with'] = json.loads(last['completed_with']) if last.get('completed_with') else None
            if last['completed_by'].startswith('SKIPPED'):
                chore_dict['was_skipped'] = True
                # Extract who skipped: "SKIPPED:PersonName" -> "PersonName"
                chore_dict['skipped_by'] = last['completed_by'].split(':', 1)[1] if ':' in last['completed_by'] else None
                skip_date = datetime.fromisoformat(last['completed_at']).date()
                chore_dict['days_since_skip'] = (today_local(household_id) - skip_date).days
                if last_actual:
                    chore_dict['last_actual_by'] = last_actual['completed_by']
                    chore_dict['days_since_actual'] = days_actual
                else:
                    chore_dict['last_actual_by'] = None
                    chore_dict['days_since_actual'] = None
            else:
                chore_dict['was_skipped'] = False
                chore_dict['skipped_by'] = None
                chore_dict['last_actual_by'] = last['completed_by']
                chore_dict['days_since_actual'] = days
        else:
            chore_dict['last_completed_by'] = None
            chore_dict['last_completed_at'] = None
            chore_dict['last_completed_with'] = None
            chore_dict['was_skipped'] = False
            chore_dict['skipped_by'] = None
            chore_dict['last_actual_by'] = None
            chore_dict['days_since_actual'] = None

        # Avoid back-to-back: if current person was the last actual completer, advance.
        # Display-only adjustment — deterministic from DB state, not persisted.
        if (people_list and len(people_list) > 1
                and chore_dict.get('last_actual_by')
                and chore_dict['current_person'] == chore_dict['last_actual_by']):
            cur_idx = chore_dict['current_person_index'] % len(people_list)
            chore_dict['current_person'] = people_list[(cur_idx + 1) % len(people_list)]

        # Calculate tomorrow's person (the next person in rotation after today's)
        if people_list and len(people_list) > 1 and chore_dict.get('also_tomorrow'):
            today_person = chore_dict['current_person']
            today_idx = people_list.index(today_person) if today_person in people_list else 0
            chore_dict['tomorrow_person'] = people_list[(today_idx + 1) % len(people_list)]
        else:
            chore_dict['tomorrow_person'] = chore_dict.get('current_person')

        result.append(chore_dict)

    return result


def calculate_streak(conn, chore_id: int, household_id: int) -> int:
    """Calculate consecutive on-time completions for a chore."""
    completions = conn.execute("""
        SELECT completed_at, completed_by FROM completions
        WHERE chore_id = ? AND completed_by NOT LIKE 'SKIPPED%' AND household_id = ?
        ORDER BY completed_at DESC
        LIMIT 30
    """, (chore_id, household_id)).fetchall()

    if not completions:
        return 0

    streak = 0
    for comp in completions:
        streak += 1

    return min(streak, 30)


@router.get("/api/chores")
def get_chores(tenant: TenantContext = Depends(get_current_user), since: str | None = Query(None)):
    """Get all chores with status, monthly stats, and weekly summary. Pass ?since=<timestamp> for delta sync."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            chores = get_chores_with_status(conn, household_id)

            # When delta syncing, filter to only chores changed since the timestamp
            if since:
                chores = [c for c in chores if c.get("updated_at") and c["updated_at"] > since]

            first_of_month = today_local(household_id).replace(day=1)
            monthly_stats = {}
            for person in get_people(household_id=household_id):
                count = conn.execute("""
                    SELECT COUNT(*) FROM completions
                    WHERE completed_by = ?
                    AND completed_at >= ?
                    AND completed_by NOT LIKE 'SKIPPED%'
                    AND household_id = ?
                """, (person, first_of_month.isoformat(), household_id)).fetchone()[0]
                monthly_stats[person] = count

            today = today_local(household_id)
            monday = today - timedelta(days=today.weekday())
            weekly_summary = [0] * 7
            for i in range(7):
                day = monday + timedelta(days=i)
                count = conn.execute("""
                    SELECT COUNT(*) FROM completions
                    WHERE date(completed_at) = ?
                    AND completed_by NOT LIKE 'SKIPPED%'
                    AND household_id = ?
                """, (day.isoformat(), household_id)).fetchone()[0]
                weekly_summary[i] = count

            for chore in chores:
                chore['streak'] = calculate_streak(conn, chore['id'], household_id)

            response = {
                "chores": chores,
                "people": get_people(household_id=household_id),
                "monthly_stats": monthly_stats,
                "weekly_summary": weekly_summary,
            }
            if since:
                deleted_rows = conn.execute(
                    "SELECT id FROM chores WHERE household_id = ? AND deleted_at > ?",
                    (household_id, since),
                ).fetchall()
                response["deleted"] = [r["id"] for r in deleted_rows]
            return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_chores: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_chores: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chores/stats")
def get_chore_stats(period: str = "week", tenant: TenantContext = Depends(get_current_user)):
    """Get chore completion stats for the household."""
    household_id = tenant.household_id
    try:
        today = today_local(household_id)
        if period == "month":
            start_date = today.replace(day=1).isoformat()
        elif period == "all":
            start_date = "2000-01-01"
        else:
            start_date = (today - timedelta(days=today.weekday())).isoformat()

        people = get_people(household_id=household_id)

        with get_db() as conn:
            chores = get_chores_with_status(conn, household_id)

            completions = conn.execute("""
                SELECT completed_by, COUNT(*) as cnt FROM completions
                WHERE household_id = ? AND completed_at >= ? AND completed_by NOT LIKE 'SKIPPED%'
                GROUP BY completed_by
            """, (household_id, start_date)).fetchall()
            comp_map = {r['completed_by']: r['cnt'] for r in completions}

            skips = conn.execute("""
                SELECT COUNT(*) as cnt FROM completions
                WHERE household_id = ? AND completed_at >= ? AND completed_by LIKE 'SKIPPED%'
            """, (household_id, start_date)).fetchone()['cnt']

            total_done = sum(comp_map.values())
            total_expected = total_done + skips + sum(1 for c in chores if c['status'] in ('overdue', 'due_today'))
            overall_rate = round((total_done / total_expected * 100) if total_expected > 0 else 100)

            # Count per-person completions for the period from DB
            person_completions = conn.execute("""
                SELECT completed_by, COUNT(*) as cnt FROM completions
                WHERE household_id = ? AND completed_at >= ? AND completed_by NOT LIKE 'SKIPPED%'
                GROUP BY completed_by
            """, (household_id, start_date)).fetchall()
            person_comp_map = {r['completed_by']: r['cnt'] for r in person_completions}

            # Count per-person currently due/overdue chores
            person_due_map: dict[str, int] = {}
            for c in chores:
                if c['status'] in ('overdue', 'due_today'):
                    for p in (c.get('people') or []):
                        person_due_map[p] = person_due_map.get(p, 0) + 1

            per_person = []
            for person in people:
                person_done = person_comp_map.get(person, 0)
                person_remaining = person_due_map.get(person, 0)
                person_expected = person_done + person_remaining
                per_person.append({
                    "name": person,
                    "completed": person_done,
                    "assigned": max(person_expected, 1),
                    "rate": min(round((person_done / max(person_expected, 1)) * 100), 100),
                })

            streaks = []
            for chore in chores:
                streak = calculate_streak(conn, chore['id'], household_id)
                if streak >= 2:
                    streaks.append({
                        "chore_name": chore['name'],
                        "streak": streak,
                        "person": chore.get('current_person', ''),
                    })
            streaks.sort(key=lambda s: s['streak'], reverse=True)

            most_skipped = conn.execute("""
                SELECT ch.name, COUNT(*) as skip_count
                FROM completions c
                JOIN chores ch ON c.chore_id = ch.id
                WHERE c.household_id = ? AND c.completed_at >= ? AND c.completed_by LIKE 'SKIPPED%'
                GROUP BY c.chore_id
                ORDER BY skip_count DESC
                LIMIT 5
            """, (household_id, start_date)).fetchall()

        return {
            "overall_rate": overall_rate,
            "per_person": per_person,
            "streaks": streaks[:5],
            "most_skipped": [{"name": r['name'], "count": r['skip_count']} for r in most_skipped],
            "period": period,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_chore_stats: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_chore_stats: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/chores/{chore_id}")
def get_chore(chore_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get a single chore."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            chore = conn.execute("SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (chore_id, household_id)).fetchone()
            if not chore:
                raise HTTPException(status_code=404, detail="Chore not found")
            chore_dict = dict(chore)
            chore_dict['people'] = json.loads(chore_dict['people'])
            return chore_dict
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/chores")
async def create_chore(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a new chore."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_chore: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_chore: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
        data['name'] = name

        description = data.get('description', '')
        if isinstance(description, str) and len(description) > 1000:
            logger.warning("Validation failed in create_chore: %s", "Description is too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Description is too long (max 1000 characters)")

        schedule_type = data.get('schedule_type', '')
        if schedule_type not in ('daily', 'days', 'interval'):
            logger.warning("Validation failed in create_chore: %s", "Invalid schedule_type")
            raise HTTPException(status_code=400, detail="Invalid schedule_type (must be 'daily', 'days', or 'interval')")

        people = data.get('people', [])
        if not isinstance(people, list):
            logger.warning("Validation failed in create_chore: %s", "people must be a list")
            raise HTTPException(status_code=400, detail="people must be a list")

        schedule_days = data.get('schedule_days', [])
        if not isinstance(schedule_days, list):
            logger.warning("Validation failed in create_chore: %s", "schedule_days must be a list")
            raise HTTPException(status_code=400, detail="schedule_days must be a list")
        # --- End validation ---

        with get_db() as conn:
            # Stagger starting index across existing chores to avoid clustering
            # Count how many chores start at each index, then pick the least-used one
            if people:
                existing = conn.execute(
                    "SELECT current_person_index FROM chores WHERE household_id = ? AND people = ?",
                    (household_id, json.dumps(people))
                ).fetchall()
                used_indexes = [r[0] % len(people) for r in existing]
                index_counts = {i: used_indexes.count(i) for i in range(len(people))}
                min_count = min(index_counts.values()) if index_counts else 0
                least_used = [i for i, c in index_counts.items() if c == min_count]
                random_start = random.choice(least_used)
            else:
                random_start = 0

            # Free tier limit: max 5 chores (only enforced post-beta)
            from tiers import FREE_TIER_LIMITS, BETA_MODE
            if not BETA_MODE:
                h_row = conn.execute("SELECT tier FROM households WHERE id = ?", (household_id,)).fetchone()
                h_tier = h_row["tier"] if h_row and h_row["tier"] else "free"
                if h_tier == "free":
                    limit = FREE_TIER_LIMITS.get("chores")
                    if limit:
                        count = conn.execute("SELECT COUNT(*) FROM chores WHERE household_id = ? AND deleted_at IS NULL", (household_id,)).fetchone()[0]
                        if count >= limit:
                            raise HTTPException(status_code=403, detail=f"Free plan is limited to {limit} chores. Upgrade to add more.")

            cursor = conn.execute("""
                INSERT INTO chores (name, description, schedule_type, schedule_days, schedule_interval, people, current_person_index, start_date, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['name'],
                data.get('description', ''),
                data['schedule_type'],
                json.dumps(data.get('schedule_days', [])),
                data.get('schedule_interval'),
                json.dumps(people),
                random_start,
                data.get('start_date'),
                household_id
            ))
            conn.commit()
            chore_id = cursor.lastrowid

        await manager.broadcast({"type": "chore_created", "chore_id": chore_id}, household_id=household_id)
        return {"id": chore_id, "message": "Chore created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/chores/{chore_id}")
async def update_chore(chore_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a chore."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in update_chore: %s", "Name is required")
            raise HTTPException(status_code=400, detail="Name is required")
        if len(name) > 200:
            logger.warning("Validation failed in update_chore: %s", "Name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Name is too long (max 200 characters)")
        data['name'] = name

        description = data.get('description', '')
        if isinstance(description, str) and len(description) > 1000:
            logger.warning("Validation failed in update_chore: %s", "Description is too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Description is too long (max 1000 characters)")

        schedule_type = data.get('schedule_type', '')
        if schedule_type not in ('daily', 'days', 'interval'):
            logger.warning("Validation failed in update_chore: %s", "Invalid schedule_type")
            raise HTTPException(status_code=400, detail="Invalid schedule_type (must be 'daily', 'days', or 'interval')")

        people = data.get('people', [])
        if not isinstance(people, list):
            logger.warning("Validation failed in update_chore: %s", "people must be a list")
            raise HTTPException(status_code=400, detail="people must be a list")

        schedule_days = data.get('schedule_days', [])
        if not isinstance(schedule_days, list):
            logger.warning("Validation failed in update_chore: %s", "schedule_days must be a list")
            raise HTTPException(status_code=400, detail="schedule_days must be a list")
        # --- End validation ---

        with get_db() as conn:
            existing = conn.execute("SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (chore_id, household_id)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Chore not found")

            check_version(data, existing, "chore")

            conn.execute("""
                UPDATE chores
                SET name = ?, description = ?, schedule_type = ?, schedule_days = ?,
                    schedule_interval = ?, people = ?,
                    version = COALESCE(version, 0) + 1,
                    updated_at = datetime('now')
                WHERE id = ? AND household_id = ?
            """, (
                data['name'],
                data.get('description', ''),
                data['schedule_type'],
                json.dumps(data.get('schedule_days', [])),
                data.get('schedule_interval'),
                json.dumps(data['people']),
                chore_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "chore_updated", "chore_id": chore_id}, household_id=household_id)
        return {"message": "Chore updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/chores/{chore_id}")
async def delete_chore(chore_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a chore."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM completions WHERE chore_id = ? AND household_id = ?", (chore_id, household_id))
            result = conn.execute("UPDATE chores SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (chore_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Chore not found")

        await manager.broadcast({"type": "chore_deleted", "chore_id": chore_id}, household_id=household_id)
        return {"message": "Chore deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


def _send_complete_chore_notifications(
    chore_id: int, chore_name: str, completed_by: str,
    completed_with: list, assigned_person: str,
    household_id: int, people: list,
):
    """Send push notifications for chore completion (runs as background task)."""
    with_text = ""
    if completed_with:
        with_text = f" with {', '.join(completed_with)}"

    # Notify the assigned person if someone else completed their chore
    if assigned_person and assigned_person != completed_by:
        send_push_to_person(
            assigned_person,
            f"Huddle: {completed_by} helped out!",
            f"{completed_by} completed '{chore_name}' for you.",
            "chore-help",
            household_id=household_id,
            module="chores",
        )

    # Notify everyone else that a chore was completed
    all_people = people
    notified = {completed_by}
    if assigned_person:
        notified.add(assigned_person)
    if completed_with:
        notified.update(completed_with)
    for_text = f" (was {assigned_person}'s)" if assigned_person and assigned_person != completed_by else ""
    for person in all_people:
        if person not in notified:
            send_push_to_person(
                person,
                f"Huddle: Chore done",
                f"{completed_by} completed '{chore_name}'{with_text}{for_text}",
                f"chore-done-{chore_id}",
                household_id=household_id,
                module="chores",
            )

    # Streak milestone notifications
    with get_db() as conn2:
        streak = calculate_streak(conn2, chore_id, household_id)
        if streak in (5, 10, 15, 20, 25, 30):
            send_push_to_person(
                completed_by,
                f"Huddle: {streak}-day streak!",
                f"You're on a {streak}-day streak for '{chore_name}'!",
                "streak",
                household_id=household_id,
                module="chores",
            )


@router.post("/api/chores/{chore_id}/complete")
async def complete_chore(chore_id: int, request: Request, background_tasks: BackgroundTasks, tenant: TenantContext = Depends(get_current_user)):
    """Mark a chore as complete."""
    try:
        household_id = tenant.household_id
        data = await request.json()
        completed_by = data.get('completed_by')
        completed_with = data.get('completed_with')

        if not completed_by:
            raise HTTPException(status_code=400, detail="completed_by is required")
        if completed_by not in get_people(household_id=household_id):
            raise HTTPException(status_code=400, detail=f"Invalid person: {completed_by}")

        if completed_with:
            people = get_people(household_id=household_id)
            completed_with = [p for p in completed_with if p in people and p != completed_by]
            completed_with_str = json.dumps(completed_with) if completed_with else None
        else:
            completed_with_str = None

        with get_db() as conn:
            chore = conn.execute("SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (chore_id, household_id)).fetchone()
            if not chore:
                raise HTTPException(status_code=404, detail="Chore not found")

            conn.execute("""
                INSERT INTO completions (chore_id, completed_by, completed_with, completed_at, household_id)
                VALUES (?, ?, ?, ?, ?)
            """, (chore_id, completed_by, completed_with_str, now_local(household_id).isoformat(), household_id))

            # Only advance rotation for solo completions — "together" completions
            # keep the same person assigned so it doesn't affect the next day
            people = json.loads(chore['people'])
            if people and not completed_with:
                current_idx = chore['current_person_index']
                new_idx = (current_idx + 1) % len(people)
                # Avoid back-to-back: don't assign to the person who just completed
                if len(people) > 1 and people[new_idx] == completed_by:
                    new_idx = (new_idx + 1) % len(people)
                conn.execute("""
                    UPDATE chores SET current_person_index = ? WHERE id = ? AND household_id = ?
                """, (new_idx, chore_id, household_id))

            conn.commit()

            # Calculate streak inside the same connection
            streak = calculate_streak(conn, chore_id, household_id)

            # Award star for chore completion (if rewards module enabled)
            try:
                mod_enabled = conn.execute(
                    "SELECT enabled FROM household_modules WHERE household_id = ? AND module_key = 'rewards'",
                    (household_id,)
                ).fetchone()
                if mod_enabled and mod_enabled["enabled"]:
                    conn.execute(
                        "INSERT INTO reward_points (person, points, reason, source_type, source_id, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (completed_by, 1, f"Completed: {chore['name']}", "chore", chore_id, household_id)
                    )
                    if completed_with:
                        for helper in completed_with:
                            conn.execute(
                                "INSERT INTO reward_points (person, points, reason, source_type, source_id, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                                (helper, 1, f"Helped with: {chore['name']}", "chore", chore_id, household_id)
                            )
                    conn.commit()
            except Exception as e:
                logger.warning("Failed to award chore completion points for %s: %s", completed_by, e)

        with_text = ""
        if completed_with:
            with_text = f" with {', '.join(completed_with)}"

        await manager.broadcast({
            "type": "chore_completed",
            "chore_id": chore_id,
            "completed_by": completed_by,
            "completed_with": completed_with or [],
            "chore_name": chore['name']
        }, household_id=household_id)

        # Determine assigned person for notification context (use updated index)
        assigned_person = None
        if people:
            idx = new_idx if (not completed_with) else chore['current_person_index']
            idx = idx % len(people)
            assigned_person = people[idx]

        # Send push notifications in the background (non-blocking)
        all_people = get_people(household_id=household_id)
        background_tasks.add_task(
            _send_complete_chore_notifications,
            chore_id=chore_id,
            chore_name=chore['name'],
            completed_by=completed_by,
            completed_with=completed_with or [],
            assigned_person=assigned_person,
            household_id=household_id,
            people=all_people,
        )

        return {"message": f"Chore completed by {completed_by}{with_text}", "streak": streak}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in complete_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in complete_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/chores/{chore_id}/skip")
async def skip_chore(chore_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Skip a chore - advances rotation and resets due date without recording completion."""
    household_id = tenant.household_id
    try:
        # Get optional skipped_by from request body
        skipped_by_name = tenant.display_name
        try:
            data = await request.json()
            if data.get('skipped_by'):
                skipped_by_name = data['skipped_by']
        except Exception:
            pass

        with get_db() as conn:
            chore = conn.execute("SELECT * FROM chores WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (chore_id, household_id)).fetchone()
            if not chore:
                raise HTTPException(status_code=404, detail="Chore not found")

            chore_dict = dict(chore)
            chore_dict['people'] = json.loads(chore_dict['people'])

            skip_date = now_local(household_id)

            if chore_dict['schedule_type'] == 'days':
                days = json.loads(chore_dict['schedule_days'] or '[]')
                today = today_local(household_id)
                for i in range(7):
                    check_date = today + timedelta(days=i)
                    day_name = check_date.strftime('%A').lower()
                    if day_name in [d.lower() for d in days]:
                        skip_date = datetime.combine(check_date, datetime.min.time())
                        break

            skip_marker = f"SKIPPED:{skipped_by_name}"
            conn.execute("""
                INSERT INTO completions (chore_id, completed_by, completed_at, household_id)
                VALUES (?, ?, ?, ?)
            """, (chore_id, skip_marker, skip_date.isoformat(), household_id))

            people = json.loads(chore['people'])
            if people:
                current_idx = chore['current_person_index']
                new_idx = (current_idx + 1) % len(people)
                conn.execute("""
                    UPDATE chores SET current_person_index = ? WHERE id = ? AND household_id = ?
                """, (new_idx, chore_id, household_id))

            conn.commit()

        await manager.broadcast({
            "type": "chore_skipped",
            "chore_id": chore_id
        }, household_id=household_id)

        return {"message": "Chore skipped"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in skip_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in skip_chore: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/chores/clear-overdue")
async def clear_overdue(tenant: TenantContext = Depends(get_current_user)):
    """Clear all overdue chores by marking them as skipped today."""
    household_id = tenant.household_id
    try:
        skip_marker = f"SKIPPED:{tenant.display_name}"
        with get_db() as conn:
            chores = get_chores_with_status(conn, household_id)
            cleared_count = 0

            for chore in chores:
                if chore['status'] == 'overdue':
                    conn.execute("""
                        INSERT INTO completions (chore_id, completed_by, completed_at, household_id)
                        VALUES (?, ?, ?, ?)
                    """, (chore['id'], skip_marker, now_local(household_id).isoformat(), household_id))
                    cleared_count += 1

            conn.commit()

        await manager.broadcast({"type": "chores_cleared"}, household_id=household_id)
        return {"message": f"Cleared {cleared_count} overdue chores", "count": cleared_count}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in clear_overdue: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in clear_overdue: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/history")
def get_history(limit: int = 50, tenant: TenantContext = Depends(get_current_user)):
    """Get completion history."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT c.*, ch.name as chore_name
                FROM completions c
                JOIN chores ch ON c.chore_id = ch.id
                WHERE ch.household_id = ?
                ORDER BY c.completed_at DESC
                LIMIT ?
            """, (household_id, limit)).fetchall()
            return {"history": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_history: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
