import calendar as cal_module
import json
import logging
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, require_role, TenantContext

from db import get_db, check_version
from settings import get_people, get_participant_color, get_participant_label, get_calendar_colors, get_setting
from websocket import manager
from push import send_push_to_person_bg

logger = logging.getLogger("huddle")

router = APIRouter()


# ============================================================================
# FAMILY CALENDAR HELPERS
# ============================================================================

def get_nth_weekday_of_month(year: int, month: int, week_of_month: int, day_of_week: int) -> Optional[date]:
    """
    Get the nth occurrence of a weekday in a month.
    week_of_month: 1-4 for 1st through 4th, -1 for last
    day_of_week: 0=Monday, 6=Sunday
    """
    import calendar

    if week_of_month == -1:
        # Last occurrence of the weekday
        last_day = calendar.monthrange(year, month)[1]
        current = date(year, month, last_day)
        while current.weekday() != day_of_week:
            current = current - timedelta(days=1)
        return current
    else:
        # nth occurrence (1st, 2nd, 3rd, 4th)
        first_day = date(year, month, 1)
        days_until_weekday = (day_of_week - first_day.weekday()) % 7
        first_occurrence = first_day + timedelta(days=days_until_weekday)

        target = first_occurrence + timedelta(weeks=week_of_month - 1)

        if target.month != month:
            return None
        return target


def generate_recurring_instances(event: dict, start_range: date, end_range: date, exceptions: set = None, *, household_id: int = None) -> list:
    """
    Generate instances of a recurring event within a date range.

    Supports frequencies:
    - daily: Every N days
    - weekly: Every N weeks (same day of week)
    - fortnightly: Every 2 weeks (14 days) - maintains day of week
    - monthly: Same day of month each month
    - monthly_nth: Nth weekday of each month (e.g., 3rd Tuesday)
    - yearly: Same date each year

    exceptions: Set of date strings (YYYY-MM-DD) to skip
    """
    instances = []
    rule = json.loads(event.get('recurrence_rule') or '{}')
    if not rule:
        return instances

    if exceptions is None:
        exceptions = set()

    frequency = rule.get('frequency', 'weekly')
    interval = rule.get('interval', 1)
    rule_end_date = rule.get('end_date')
    count = rule.get('count')

    # For monthly_nth pattern
    week_of_month = rule.get('week_of_month')
    day_of_week = rule.get('day_of_week')

    event_start = date.fromisoformat(event['start_date'])

    # Determine the effective end date for recurrence
    if rule_end_date:
        recurrence_end = min(date.fromisoformat(rule_end_date), end_range)
    else:
        recurrence_end = end_range

    # Calculate interval in days based on frequency
    if frequency == 'daily':
        delta_days = interval
    elif frequency == 'weekly':
        delta_days = 7 * interval
    elif frequency == 'fortnightly':
        delta_days = 14
    elif frequency == 'monthly' or frequency == 'monthly_nth':
        delta_days = None
    elif frequency == 'yearly':
        delta_days = None
    else:
        return instances

    current_date = event_start
    instance_count = 0
    total_count = 0
    max_iterations = 1000

    while current_date <= recurrence_end and max_iterations > 0:
        max_iterations -= 1

        date_str = current_date.isoformat()

        if date_str not in exceptions:
            if current_date >= start_range:
                instance = {
                    'id': event['id'],
                    'title': event['title'],
                    'description': event.get('description'),
                    'date': date_str,
                    'start_time': event.get('start_time'),
                    'end_time': event.get('end_time'),
                    'all_day': event.get('all_day', 1) == 1,
                    'participants': json.loads(event.get('participants') or '[]'),
                    'color': get_participant_color(json.loads(event.get('participants') or '[]'), household_id=household_id),
                    'is_recurring': True,
                    'event_type': 'recurring',
                    'recurrence_date': date_str
                }
                instances.append(instance)
                instance_count += 1

        total_count += 1
        if count and total_count >= count:
            break

        # Calculate next occurrence
        if frequency == 'monthly':
            month = current_date.month + interval
            year = current_date.year + (month - 1) // 12
            month = ((month - 1) % 12) + 1
            import calendar
            max_day = calendar.monthrange(year, month)[1]
            day = min(event_start.day, max_day)
            try:
                current_date = date(year, month, day)
            except ValueError:
                break
        elif frequency == 'monthly_nth':
            if week_of_month is None or day_of_week is None:
                week_of_month = (event_start.day - 1) // 7 + 1
                day_of_week = event_start.weekday()

            month = current_date.month + interval
            year = current_date.year + (month - 1) // 12
            month = ((month - 1) % 12) + 1

            next_date = get_nth_weekday_of_month(year, month, week_of_month, day_of_week)
            if next_date is None:
                break
            current_date = next_date
        elif frequency == 'yearly':
            try:
                import calendar
                next_year = current_date.year + interval
                if event_start.month == 2 and event_start.day == 29:
                    if calendar.isleap(next_year):
                        current_date = date(next_year, 2, 29)
                    else:
                        current_date = date(next_year, 2, 28)
                else:
                    current_date = date(next_year, event_start.month, event_start.day)
            except ValueError:
                break
        else:
            current_date = current_date + timedelta(days=delta_days)

    return instances


# ============================================================================
# FAMILY CALENDAR API - Local calendar events for the household
# ============================================================================

@router.get("/api/family-calendar/month/{year}/{month}")
def get_family_calendar_month(year: int, month: int, tenant: TenantContext = Depends(get_current_user)):
    """Get all family calendar events for a specific month."""
    household_id = tenant.household_id
    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        LOCAL_TZ = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))

        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)

        range_start = first_day - timedelta(days=7)
        range_end = last_day + timedelta(days=7)

        events = []

        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM calendar_events
                WHERE household_id = ? AND deleted_at IS NULL
                  AND ((start_date <= ? AND (end_date >= ? OR end_date IS NULL OR event_type = 'recurring'))
                   OR (start_date >= ? AND start_date <= ?))
                ORDER BY start_date, start_time
            """, (household_id, range_end.isoformat(), range_start.isoformat(),
                  range_start.isoformat(), range_end.isoformat())).fetchall()

            exception_rows = conn.execute("""
                SELECT ce.event_id, ce.exception_date FROM calendar_event_exceptions ce
                JOIN calendar_events e ON e.id = ce.event_id
                WHERE e.household_id = ? AND ce.exception_date >= ? AND ce.exception_date <= ?
            """, (household_id, first_day.isoformat(), last_day.isoformat())).fetchall()

            exceptions_by_event = {}
            for exc in exception_rows:
                event_id = exc['event_id']
                if event_id not in exceptions_by_event:
                    exceptions_by_event[event_id] = set()
                exceptions_by_event[event_id].add(exc['exception_date'])

            for row in rows:
                event = dict(row)
                event_type = event.get('event_type', 'one_off')
                participants = json.loads(event.get('participants') or '[]')
                color = get_participant_color(participants, household_id=household_id)

                if event_type == 'recurring':
                    event_exceptions = exceptions_by_event.get(event['id'], set())
                    instances = generate_recurring_instances(event, first_day, last_day, event_exceptions, household_id=household_id)
                    events.extend(instances)
                elif event_type == 'multi_day':
                    start = date.fromisoformat(event['start_date'])
                    end = date.fromisoformat(event.get('end_date') or event['start_date'])

                    current = max(start, first_day)
                    while current <= min(end, last_day):
                        events.append({
                            'id': event['id'],
                            'title': event['title'],
                            'description': event.get('description'),
                            'date': current.isoformat(),
                            'start_time': event.get('start_time') if current == start else None,
                            'end_time': event.get('end_time') if current == end else None,
                            'all_day': event.get('all_day', 1) == 1,
                            'participants': participants,
                            'color': color,
                            'is_multi_day': True,
                            'is_start': current == start,
                            'is_end': current == end,
                            'event_type': 'multi_day'
                        })
                        current = current + timedelta(days=1)
                else:
                    event_date = date.fromisoformat(event['start_date'])
                    if first_day <= event_date <= last_day:
                        events.append({
                            'id': event['id'],
                            'title': event['title'],
                            'description': event.get('description'),
                            'date': event['start_date'],
                            'start_time': event.get('start_time'),
                            'end_time': event.get('end_time'),
                            'all_day': event.get('all_day', 1) == 1,
                            'participants': participants,
                            'color': color,
                            'event_type': 'one_off'
                        })

        events.sort(key=lambda x: (x['date'], x.get('start_time') or '00:00'))

        today = datetime.now(LOCAL_TZ).date()

        return {
            "year": year,
            "month": month,
            "month_name": first_day.strftime('%B'),
            "first_day_weekday": first_day.weekday(),
            "days_in_month": last_day.day,
            "today": today.isoformat(),
            "is_current_month": today.year == year and today.month == month,
            "events": events,
            "colors": get_calendar_colors(household_id=household_id)
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_family_calendar_month: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_family_calendar_month: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/family-calendar/events")
def get_family_calendar_events(start_date: str = None, end_date: str = None, tenant: TenantContext = Depends(get_current_user)):
    """Get family calendar events for a date range."""
    household_id = tenant.household_id
    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        LOCAL_TZ = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id))
        today = datetime.now(LOCAL_TZ).date()

        if not start_date:
            start_date = today.isoformat()
        if not end_date:
            end_date = (today + timedelta(days=30)).isoformat()

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)

        events = []

        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM calendar_events
                WHERE household_id = ? AND deleted_at IS NULL
                  AND ((start_date <= ? AND (end_date >= ? OR end_date IS NULL OR event_type = 'recurring'))
                   OR (start_date >= ? AND start_date <= ?))
                ORDER BY start_date, start_time
            """, (household_id, end.isoformat(), start.isoformat(),
                  start.isoformat(), end.isoformat())).fetchall()

            for row in rows:
                event = dict(row)
                event['participants'] = json.loads(event.get('participants') or '[]')
                event['recurrence_rule'] = json.loads(event.get('recurrence_rule') or 'null')
                event['color'] = get_participant_color(event['participants'], household_id=household_id)
                event['participant_label'] = get_participant_label(event['participants'], household_id=household_id)
                events.append(event)

        return {"events": events}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_family_calendar_events: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_family_calendar_events: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/family-calendar/events/{event_id}")
def get_family_calendar_event(event_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get a single family calendar event."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM calendar_events WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (event_id, household_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Event not found")

            event = dict(row)
            event['participants'] = json.loads(event.get('participants') or '[]')
            event['recurrence_rule'] = json.loads(event.get('recurrence_rule') or 'null')
            event['color'] = get_participant_color(event['participants'])
            event['participant_label'] = get_participant_label(event['participants'])
            return event
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/family-calendar/events")
async def create_family_calendar_event(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a new family calendar event."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        title = data.get('title', '').strip() if isinstance(data.get('title'), str) else ''
        if not title:
            logger.warning("Validation failed in create_family_calendar_event: %s", "Title is required")
            raise HTTPException(status_code=400, detail="Title is required")
        if len(title) > 200:
            logger.warning("Validation failed in create_family_calendar_event: %s", "Title is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")
        data['title'] = title

        description = data.get('description', '')
        if isinstance(description, str) and len(description) > 1000:
            logger.warning("Validation failed in create_family_calendar_event: %s", "Description is too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Description is too long (max 1000 characters)")

        if not data.get('start_date'):
            logger.warning("Validation failed in create_family_calendar_event: %s", "Start date is required")
            raise HTTPException(status_code=400, detail="Start date is required")
        if not data.get('participants'):
            logger.warning("Validation failed in create_family_calendar_event: %s", "At least one participant is required")
            raise HTTPException(status_code=400, detail="At least one participant is required")

        participants = data.get('participants', [])
        if not isinstance(participants, list):
            logger.warning("Validation failed in create_family_calendar_event: %s", "participants must be a list")
            raise HTTPException(status_code=400, detail="participants must be a list")

        event_type = data.get('event_type', 'one_off')

        recurrence_rule = None
        if event_type == 'recurring' and data.get('recurrence_rule'):
            recurrence_rule = json.dumps(data['recurrence_rule'])

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO calendar_events
                (title, description, event_type, start_date, end_date, start_time, end_time,
                 all_day, participants, recurrence_rule, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['title'],
                data.get('description', ''),
                event_type,
                data['start_date'],
                data.get('end_date'),
                data.get('start_time'),
                data.get('end_time'),
                1 if data.get('all_day', True) else 0,
                json.dumps(participants),
                recurrence_rule,
                household_id
            ))
            conn.commit()
            event_id = cursor.lastrowid

        await manager.broadcast({"type": "calendar_updated"}, household_id=household_id)

        # Notify participants about the new event
        try:
            time_text = f" at {data.get('start_time')}" if data.get('start_time') else ""
            date_text = data.get('start_date', '')
            all_people = get_people(household_id=household_id)
            for person in participants:
                if person != tenant.display_name and person in all_people:
                    send_push_to_person_bg(
                        person,
                        "Huddle: New event",
                        f"{title} on {date_text}{time_text}",
                        "calendar-new",
                        household_id=household_id,
                        module="calendar",
                    )
            # Also notify "Everyone" participants
            if "Everyone" in participants:
                for person in all_people:
                    if person != tenant.display_name and person not in participants:
                        send_push_to_person_bg(
                            person,
                            "Huddle: New event",
                            f"{title} on {date_text}{time_text}",
                            "calendar-new",
                            household_id=household_id,
                            module="calendar",
                        )
        except Exception as push_err:
            logger.warning("Calendar push notification failed: %s", push_err)

        return {"id": event_id, "message": "Event created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/family-calendar/events/{event_id}")
async def update_family_calendar_event(event_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a family calendar event."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        if 'title' in data:
            title = data.get('title', '').strip() if isinstance(data.get('title'), str) else ''
            if not title:
                logger.warning("Validation failed in update_family_calendar_event: %s", "Title cannot be empty")
                raise HTTPException(status_code=400, detail="Title cannot be empty")
            if len(title) > 200:
                logger.warning("Validation failed in update_family_calendar_event: %s", "Title is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail="Title is too long (max 200 characters)")
            data['title'] = title

        if 'description' in data:
            description = data.get('description', '')
            if isinstance(description, str) and len(description) > 1000:
                logger.warning("Validation failed in update_family_calendar_event: %s", "Description is too long (max 1000 characters)")
                raise HTTPException(status_code=400, detail="Description is too long (max 1000 characters)")

        if 'participants' in data and not isinstance(data['participants'], list):
            logger.warning("Validation failed in update_family_calendar_event: %s", "participants must be a list")
            raise HTTPException(status_code=400, detail="participants must be a list")
        # --- End validation ---

        with get_db() as conn:
            existing = conn.execute("SELECT * FROM calendar_events WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (event_id, household_id)).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Event not found")
            check_version(data, existing, "calendar event")

            event_type = data.get('event_type', existing['event_type'])
            participants = data.get('participants', json.loads(existing['participants'] or '[]'))

            recurrence_rule = None
            if event_type == 'recurring' and data.get('recurrence_rule'):
                recurrence_rule = json.dumps(data['recurrence_rule'])
            elif event_type == 'recurring' and existing['recurrence_rule']:
                recurrence_rule = existing['recurrence_rule']

            conn.execute("""
                UPDATE calendar_events SET
                    title = ?,
                    description = ?,
                    event_type = ?,
                    start_date = ?,
                    end_date = ?,
                    start_time = ?,
                    end_time = ?,
                    all_day = ?,
                    participants = ?,
                    recurrence_rule = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    version = COALESCE(version, 0) + 1
                WHERE id = ? AND household_id = ?
            """, (
                data.get('title', existing['title']),
                data.get('description', existing['description']),
                event_type,
                data.get('start_date', existing['start_date']),
                data.get('end_date', existing['end_date']),
                data.get('start_time', existing['start_time']),
                data.get('end_time', existing['end_time']),
                1 if data.get('all_day', existing['all_day']) else 0,
                json.dumps(participants),
                recurrence_rule,
                event_id,
                household_id
            ))
            conn.commit()

        await manager.broadcast({"type": "calendar_updated"}, household_id=household_id)
        return {"message": "Event updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/family-calendar/events/{event_id}")
async def delete_family_calendar_event(event_id: int, delete_type: str = "all", occurrence_date: str = None, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """
    Delete a family calendar event or specific occurrences.

    Query parameters:
    - delete_type: "all" (entire series), "this" (single occurrence), "future" (this and all future)
    - occurrence_date: Required for "this" and "future" delete types (YYYY-MM-DD)
    """
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            event = conn.execute("SELECT * FROM calendar_events WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (event_id, household_id)).fetchone()
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")

            event_type = event['event_type']

            if delete_type == "all":
                conn.execute("DELETE FROM calendar_event_exceptions WHERE event_id = ?", (event_id,))
                conn.execute("UPDATE calendar_events SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (event_id, household_id))
                conn.commit()
                message = "Event series deleted"

            elif delete_type == "this" and event_type == "recurring":
                if not occurrence_date:
                    raise HTTPException(status_code=400, detail="occurrence_date is required for deleting single occurrence")

                try:
                    date.fromisoformat(occurrence_date)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

                conn.execute("""
                    INSERT OR IGNORE INTO calendar_event_exceptions (event_id, exception_date, exception_type)
                    VALUES (?, ?, 'deleted')
                """, (event_id, occurrence_date))
                conn.commit()
                message = f"Occurrence on {occurrence_date} deleted"

            elif delete_type == "future" and event_type == "recurring":
                if not occurrence_date:
                    raise HTTPException(status_code=400, detail="occurrence_date is required for deleting future occurrences")

                try:
                    occ_date = date.fromisoformat(occurrence_date)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

                new_end = (occ_date - timedelta(days=1)).isoformat()

                rule = json.loads(event['recurrence_rule'] or '{}')
                rule['end_date'] = new_end
                conn.execute("""
                    UPDATE calendar_events SET recurrence_rule = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND household_id = ?
                """, (json.dumps(rule), event_id, household_id))

                conn.execute("""
                    DELETE FROM calendar_event_exceptions
                    WHERE event_id = ? AND exception_date >= ?
                """, (event_id, occurrence_date))
                conn.commit()
                message = f"This and all future occurrences deleted"

            else:
                conn.execute("UPDATE calendar_events SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?", (event_id, household_id))
                conn.commit()
                message = "Event deleted"

        await manager.broadcast({"type": "calendar_updated"}, household_id=household_id)
        return {"message": message}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_family_calendar_event: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/family-calendar/events/{event_id}/exceptions")
def get_event_exceptions(event_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get all exceptions (deleted instances) for a recurring event."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            event = conn.execute("SELECT * FROM calendar_events WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (event_id, household_id)).fetchone()
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")

            exceptions = conn.execute("""
                SELECT * FROM calendar_event_exceptions
                WHERE event_id = ?
                ORDER BY exception_date
            """, (event_id,)).fetchall()

            return {
                "event_id": event_id,
                "exceptions": [dict(e) for e in exceptions]
            }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_event_exceptions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_event_exceptions: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/family-calendar/events/{event_id}/exceptions")
async def add_event_exception(event_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Add an exception (delete a specific occurrence) for a recurring event."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        exception_date = data.get('exception_date')

        if not exception_date:
            raise HTTPException(status_code=400, detail="exception_date is required")

        try:
            date.fromisoformat(exception_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

        with get_db() as conn:
            event = conn.execute("SELECT * FROM calendar_events WHERE id = ? AND household_id = ? AND deleted_at IS NULL", (event_id, household_id)).fetchone()
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")

            if event['event_type'] != 'recurring':
                raise HTTPException(status_code=400, detail="Exceptions can only be added to recurring events")

            try:
                conn.execute("""
                    INSERT INTO calendar_event_exceptions (event_id, exception_date, exception_type)
                    VALUES (?, ?, 'deleted')
                """, (event_id, exception_date))
                conn.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=400, detail="Exception already exists for this date")

        await manager.broadcast({"type": "calendar_updated"}, household_id=household_id)
        return {"message": f"Exception added for {exception_date}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in add_event_exception: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in add_event_exception: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/family-calendar/events/{event_id}/exceptions/{exception_date}")
async def remove_event_exception(event_id: int, exception_date: str, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Remove an exception (restore a previously deleted occurrence)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("""
                DELETE FROM calendar_event_exceptions
                WHERE event_id = ? AND exception_date = ?
            """, (event_id, exception_date))
            conn.commit()

            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Exception not found")

        await manager.broadcast({"type": "calendar_updated"}, household_id=household_id)
        return {"message": f"Exception removed for {exception_date}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in remove_event_exception: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in remove_event_exception: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
