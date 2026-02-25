"""Background tasks for systemd watchdog, screen management, and push notifications."""
import asyncio
import logging
import time
import gc
from datetime import datetime, date, timedelta

logger = logging.getLogger("huddle")

# Systemd watchdog support
try:
    import sdnotify
    SYSTEMD_NOTIFY = sdnotify.SystemdNotifier()
except ImportError:
    SYSTEMD_NOTIFY = None

from config import PI_SCREEN_CONTROL
from db import get_db
from settings import get_setting, get_all_settings, get_people
from websocket import manager
from push import send_push_to_person, PUSH_ENABLED, VAPID_PRIVATE_KEY


def _get_all_household_ids():
    """Return list of all household IDs."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT id FROM households").fetchall()
            return [r["id"] for r in rows]
    except Exception as e:
        logger.error("Failed to query household IDs: %s", e, exc_info=True)
        return []


def _get_kiosk_household_ids():
    """Return list of household IDs that have kiosk tokens (i.e. have a display)."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT household_id FROM kiosk_tokens").fetchall()
            return [r["household_id"] for r in rows]
    except Exception as e:
        logger.error("Failed to query kiosk household IDs: %s", e, exc_info=True)
        return []


async def watchdog_and_maintenance_task():
    """Background task that sends systemd watchdog pings and performs maintenance."""
    # Explicit timestamps instead of fragile modulo arithmetic
    _last_gc = 0.0
    _last_pairing_cleanup = 0.0
    _last_page_flush = 0.0

    GC_INTERVAL = 300           # 5 minutes
    PAIRING_CLEANUP_INTERVAL = 600  # 10 minutes
    PAGE_FLUSH_INTERVAL = 300   # 5 minutes

    while True:
        try:
            now = time.time()

            # Send systemd watchdog notification
            if SYSTEMD_NOTIFY:
                SYSTEMD_NOTIFY.notify("WATCHDOG=1")

            # Periodic garbage collection every 5 minutes
            if now - _last_gc >= GC_INTERVAL:
                _last_gc = now
                gc.collect()

            # Clean expired pairing codes every ~10 minutes
            if now - _last_pairing_cleanup >= PAIRING_CLEANUP_INTERVAL:
                _last_pairing_cleanup = now
                try:
                    with get_db() as conn:
                        conn.execute("DELETE FROM kiosk_pairing_codes WHERE expires_at < datetime('now', '-1 hour')")
                        conn.commit()
                except Exception as e:
                    logger.warning("Failed to clean expired pairing codes: %s", e)

            # Flush page view counts every ~5 minutes
            if now - _last_page_flush >= PAGE_FLUSH_INTERVAL:
                _last_page_flush = now
                try:
                    from page_views import flush_page_views
                    await flush_page_views()
                except Exception as e:
                    logger.warning("Failed to flush page views: %s", e)

        except Exception as e:
            logger.error("Watchdog task error: %s", e, exc_info=True)

        await asyncio.sleep(30)  # Send watchdog every 30 seconds (interval is 60s)


async def service_monitor_task():
    """Background task to monitor services and keep screen/data fresh."""
    from fuel import fuel_cache, FUEL_CACHE_TTL, parse_fuel_cache_file

    # Track last fuel refresh
    last_fuel_check = 0
    FUEL_CHECK_INTERVAL = 300  # Check fuel freshness every 5 minutes
    # Track screen state per household to only broadcast on changes
    last_screen_states = {}  # household_id -> bool

    while True:
        try:
            now = time.time()

            # Screen keepalive - broadcast to kiosk only on state changes
            if PI_SCREEN_CONTROL:
                try:
                    from zoneinfo import ZoneInfo

                    for hid in _get_kiosk_household_ids():
                        try:
                            # Batch-fetch all settings for this household (1 DB call instead of 3)
                            all_settings = get_all_settings(household_id=hid)
                            tz_name = all_settings.get("timezone", "Australia/Sydney")
                            aest = ZoneInfo(tz_name)
                            current_hour = datetime.now(aest).hour
                            current_min = datetime.now(aest).minute
                            current_time = current_hour * 60 + current_min

                            # Parse screen schedule from batch-fetched settings
                            on_time = all_settings.get("screen_on_time", "06:30")
                            off_time = all_settings.get("screen_off_time", "21:00")
                            on_parts = on_time.split(':')
                            off_parts = off_time.split(':')
                            on_minutes = int(on_parts[0]) * 60 + int(on_parts[1])
                            off_minutes = int(off_parts[0]) * 60 + int(off_parts[1])

                            screen_should_be_on = on_minutes <= current_time < off_minutes
                            if screen_should_be_on != last_screen_states.get(hid):
                                last_screen_states[hid] = screen_should_be_on
                                if screen_should_be_on:
                                    await manager.broadcast({"type": "screen_on"}, household_id=hid)
                                else:
                                    await manager.broadcast({"type": "screen_off"}, household_id=hid)
                        except Exception as e:
                            logger.warning("Screen scheduling error for household %d: %s", hid, e)
                except Exception as e:
                    logger.warning("Screen scheduling error: %s", e)

            # Fuel data freshness check
            if now - last_fuel_check > FUEL_CHECK_INTERVAL:
                last_fuel_check = now
                # Pre-warm fuel cache if stale
                if fuel_cache["data"] is None or (now - fuel_cache["timestamp"]) > FUEL_CACHE_TTL:
                    cache_data = parse_fuel_cache_file()
                    if cache_data:
                        fuel_cache["data"] = cache_data
                        fuel_cache["timestamp"] = now
                        logger.debug("Service monitor: Fuel cache refreshed")
                        for hid in _get_all_household_ids():
                            await manager.broadcast({"type": "fuel_updated"}, household_id=hid)

        except Exception as e:
            logger.error("Service monitor error: %s", e, exc_info=True)

        await asyncio.sleep(30)  # Run every 30 seconds


async def push_notification_task():
    """Send scheduled push notifications. Runs every 15 minutes."""
    # Import here to avoid circular imports
    from routers.chores import get_chores_with_status
    from routers.calendar import generate_recurring_instances
    import json

    # Track per-household state
    last_morning_dates = {}   # household_id -> date_str
    last_routine_dates = {}
    last_selfcare_dates = {}
    last_evening_dates = {}
    last_meal_dates = {}
    last_weekly_dates = {}

    while True:
        try:
            if PUSH_ENABLED and VAPID_PRIVATE_KEY:
                try:
                    from zoneinfo import ZoneInfo
                except ImportError:
                    from backports.zoneinfo import ZoneInfo

                for hid in _get_all_household_ids():
                    try:
                        await asyncio.to_thread(
                            _send_household_notifications,
                            hid, ZoneInfo, get_chores_with_status,
                            generate_recurring_instances, json,
                            last_morning_dates, last_routine_dates,
                            last_selfcare_dates, last_evening_dates,
                            last_meal_dates, last_weekly_dates,
                        )
                    except Exception as e:
                        logger.error("Push notification error for household %d: %s", hid, e, exc_info=True)

        except Exception as e:
            logger.error("Push notification task error: %s", e, exc_info=True)

        await asyncio.sleep(900)  # Check every 15 minutes


def _send_household_notifications(
    hid, ZoneInfo, get_chores_with_status,
    generate_recurring_instances, json,
    last_morning_dates, last_routine_dates,
    last_selfcare_dates, last_evening_dates,
    last_meal_dates, last_weekly_dates,
):
    """Send scheduled notifications for a single household."""
    # Batch-fetch all settings once (1 DB call instead of 8-10 per-key calls)
    all_settings = get_all_settings(household_id=hid)

    tz_name = all_settings.get("timezone", "Australia/Sydney")
    aest = ZoneInfo(tz_name)
    now = datetime.now(aest)
    current_time = now.hour * 60 + now.minute
    today_str = now.strftime('%Y-%m-%d')
    tomorrow = (now + timedelta(days=1)).date()
    tomorrow_str = tomorrow.isoformat()

    # Parse notification times from batch-fetched settings
    def _parse_time(key, default_minutes):
        t = all_settings.get(key)
        if t and ':' in t:
            parts = t.split(':')
            return int(parts[0]) * 60 + int(parts[1])
        return default_minutes

    morning_time = _parse_time("notify_morning", 450)   # 7:30
    afternoon_time = _parse_time("notify_afternoon", 960)  # 16:00
    evening_time = _parse_time("notify_evening", 1140)  # 19:00

    people = get_people(household_id=hid)
    if not people:
        return  # No members configured, skip

    # ── Morning: Consolidated digest (chores + routines + selfcare) ──
    if morning_time <= current_time < morning_time + 15 and last_morning_dates.get(hid) != today_str:
        last_morning_dates[hid] = today_str
        last_routine_dates[hid] = today_str
        last_selfcare_dates[hid] = today_str
        logger.info("Sending morning digest for household %d...", hid)

        # Build per-person digest parts
        digest = {p: [] for p in people}  # person -> list of body lines

        try:
            with get_db() as conn:
                # Chores
                if all_settings.get("notify_chores", "true") == "true":
                    chores = get_chores_with_status(conn, household_id=hid)
                    for person in people:
                        due = [c for c in chores if c['current_person'] == person and c['status'] == 'due_today']
                        overdue = [c for c in chores if c['current_person'] == person and c['status'] == 'overdue']
                        if overdue:
                            names = ', '.join(c['name'] for c in overdue[:3])
                            if len(overdue) > 3:
                                names += f' +{len(overdue) - 3} more'
                            digest[person].append(f"\u26a0\ufe0f OVERDUE: {names}")
                        if due:
                            names = ', '.join(c['name'] for c in due[:3])
                            if len(due) > 3:
                                names += f' +{len(due) - 3} more'
                            digest[person].append(f"\U0001f9f9 Due: {names}")

                    # Bills due today (shared across household)
                    bills_today = conn.execute(
                        "SELECT name, amount FROM bills WHERE paid=0 AND due_date=? AND household_id=?",
                        (today_str, hid)
                    ).fetchall()
                    if bills_today:
                        bill_text = ', '.join(
                            f"{b['name']} (${b['amount']:.0f})" if b['amount'] else b['name']
                            for b in bills_today
                        )
                        for person in people:
                            digest[person].append(f"\U0001f4b0 Bills: {bill_text}")

                # Routines — single query with JOINs instead of N+1
                if all_settings.get("notify_routines", "true") == "true":
                    mod_enabled = conn.execute(
                        "SELECT enabled FROM household_modules WHERE household_id = ? AND module_key = 'routines'",
                        (hid,)
                    ).fetchone()
                    if mod_enabled and mod_enabled["enabled"]:
                        routine_rows = conn.execute("""
                            SELECT r.id, r.name, r.assigned_to, r.routine_type,
                                   COUNT(ri.id) AS item_count,
                                   COUNT(DISTINCT rc.item_id) AS done_count
                            FROM routines r
                            LEFT JOIN routine_items ri ON ri.routine_id = r.id
                            LEFT JOIN routine_completions rc
                                ON rc.routine_id = r.id AND rc.item_id = ri.id
                                AND rc.completed_date = ?
                            WHERE r.household_id = ?
                              AND r.routine_type IN ('morning', 'afternoon')
                            GROUP BY r.id
                            HAVING item_count > 0 AND done_count < item_count
                        """, (today_str, hid)).fetchall()
                        for routine in routine_rows:
                            remaining = routine["item_count"] - routine["done_count"]
                            emoji = {"morning": "\u2600\ufe0f", "afternoon": "\U0001f324\ufe0f"}.get(routine["routine_type"], "\U0001f4cb")
                            person = routine["assigned_to"]
                            if person in digest:
                                digest[person].append(f"{emoji} {routine['name']}: {remaining} left")

                # Self care — single query with LEFT JOIN instead of N+1
                if all_settings.get("notify_selfcare", "true") == "true":
                    mod_enabled = conn.execute(
                        "SELECT enabled FROM household_modules WHERE household_id = ? AND module_key = 'selfcare'",
                        (hid,)
                    ).fetchone()
                    if mod_enabled and mod_enabled["enabled"]:
                        sc_rows = conn.execute("""
                            SELECT si.id, si.name, si.icon, si.assigned_to, si.frequency_days,
                                   MAX(sl.logged_at) AS last_logged_at
                            FROM selfcare_items si
                            LEFT JOIN selfcare_logs sl
                                ON sl.item_id = si.id AND sl.household_id = si.household_id
                            WHERE si.household_id = ? AND si.frequency_days > 0
                            GROUP BY si.id
                        """, (hid,)).fetchall()
                        for item in sc_rows:
                            is_due = True
                            if item["last_logged_at"]:
                                last_date = datetime.fromisoformat(item["last_logged_at"]).date()
                                days_since = (now.date() - last_date).days
                                is_due = days_since >= item["frequency_days"]
                            if is_due:
                                icon = item["icon"] or "\U0001f48a"
                                person = item["assigned_to"]
                                if person in digest:
                                    digest[person].append(f"{icon} {item['name']}")

        except Exception as e:
            logger.warning("Morning digest data error for household %d: %s", hid, e)

        # Send one notification per person
        for person, parts in digest.items():
            if not parts:
                continue
            body = '\n'.join(parts)
            count = len(parts)
            title = f"Huddle: Good morning! {count} item{'s' if count != 1 else ''} today"
            send_push_to_person(person, title, body, f"daily-{today_str}", household_id=hid, module="chores")
            logger.info("  Morning digest to %s: %d items", person, count)

    # ── Afternoon: Meal plan reminder (tonight's dinner) ──
    if afternoon_time <= current_time < afternoon_time + 15 and last_meal_dates.get(hid) != today_str and all_settings.get("notify_meals", "true") == "true":
        last_meal_dates[hid] = today_str
        logger.info("Sending meal plan notifications for household %d...", hid)

        today_weekday = now.weekday()  # 0=Monday
        with get_db() as conn:
            dinners = conn.execute("""
                SELECT meal_name, meal_variant FROM meals
                WHERE day_of_week = ? AND meal_type = 'dinner' AND household_id = ?
            """, (today_weekday, hid)).fetchall()

            if dinners:
                for dinner in dinners:
                    variant = dinner['meal_variant']
                    meal_name = dinner['meal_name']
                    if variant == 'all' or not variant:
                        for person in people:
                            send_push_to_person(person, "Huddle: Tonight's dinner", meal_name, "meal", household_id=hid, module="meals")
                    else:
                        try:
                            people_list = json.loads(variant)
                            for p in people_list:
                                send_push_to_person(p, "Huddle: Tonight's dinner", meal_name, "meal", household_id=hid, module="meals")
                        except (json.JSONDecodeError, TypeError):
                            for person in people:
                                if person.lower() in variant.lower():
                                    send_push_to_person(person, "Huddle: Tonight's dinner", meal_name, "meal", household_id=hid, module="meals")
                logger.info("  Meal notifications sent for household %d", hid)

    # ── Evening: Bedtime routine reminders (single query with JOINs) ──
    if evening_time <= current_time < evening_time + 15 and all_settings.get("notify_routines", "true") == "true":
        try:
            with get_db() as conn:
                mod_enabled = conn.execute(
                    "SELECT enabled FROM household_modules WHERE household_id = ? AND module_key = 'routines'",
                    (hid,)
                ).fetchone()
                if mod_enabled and mod_enabled["enabled"]:
                    routine_rows = conn.execute("""
                        SELECT r.id, r.name, r.assigned_to,
                               COUNT(ri.id) AS item_count,
                               COUNT(DISTINCT rc.item_id) AS done_count
                        FROM routines r
                        LEFT JOIN routine_items ri ON ri.routine_id = r.id
                        LEFT JOIN routine_completions rc
                            ON rc.routine_id = r.id AND rc.item_id = ri.id
                            AND rc.completed_date = ?
                        WHERE r.household_id = ? AND r.routine_type = 'bedtime'
                        GROUP BY r.id
                        HAVING item_count > 0 AND done_count < item_count
                    """, (today_str, hid)).fetchall()
                    for routine in routine_rows:
                        remaining = routine["item_count"] - routine["done_count"]
                        send_push_to_person(
                            routine["assigned_to"],
                            f"Huddle: {routine['name']}",
                            f"{remaining} item{'s' if remaining != 1 else ''} left tonight",
                            f"routine-bedtime-{today_str}",
                            household_id=hid,
                            module="routines",
                        )
        except Exception as e:
            logger.warning("Bedtime routine push error for household %d: %s", hid, e)

    # ── Evening: Calendar reminders for tomorrow ──
    if evening_time <= current_time < evening_time + 15 and last_evening_dates.get(hid) != today_str and all_settings.get("notify_calendar", "true") == "true":
        last_evening_dates[hid] = today_str
        logger.info("Sending calendar reminder notifications for household %d...", hid)

        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM calendar_events
                WHERE household_id = ? AND (
                    (event_type = 'one_off' AND start_date = ?)
                    OR (event_type = 'multi_day' AND start_date <= ? AND end_date >= ?)
                    OR event_type = 'recurring'
                )
            """, (hid, tomorrow_str, tomorrow_str, tomorrow_str)).fetchall()

            exc_rows = conn.execute("""
                SELECT ce.event_id, ce.exception_date FROM calendar_event_exceptions ce
                JOIN calendar_events e ON e.id = ce.event_id
                WHERE e.household_id = ? AND ce.exception_date = ?
            """, (hid, tomorrow_str)).fetchall()
            exc_by_event = {}
            for e in exc_rows:
                exc_by_event.setdefault(e['event_id'], set()).add(e['exception_date'])

            tomorrow_events = []
            for row in rows:
                event = dict(row)
                if event['event_type'] == 'recurring':
                    instances = generate_recurring_instances(event, tomorrow, tomorrow, exc_by_event.get(event['id'], set()), household_id=hid)
                    tomorrow_events.extend(instances)
                elif event['event_type'] == 'multi_day':
                    start = date.fromisoformat(event['start_date'])
                    end = date.fromisoformat(event.get('end_date') or event['start_date'])
                    if start <= tomorrow <= end:
                        tomorrow_events.append({
                            'title': event['title'],
                            'start_time': event.get('start_time'),
                            'participants': json.loads(event.get('participants') or '[]')
                        })
                else:
                    tomorrow_events.append({
                        'title': event['title'],
                        'start_time': event.get('start_time'),
                        'participants': json.loads(event.get('participants') or '[]')
                    })

            if tomorrow_events:
                for person in people:
                    person_events = [e for e in tomorrow_events
                                     if person in e.get('participants', [])
                                     or 'Everyone' in e.get('participants', [])]
                    if not person_events:
                        continue

                    if len(person_events) == 1:
                        ev = person_events[0]
                        time_str = f" at {ev['start_time']}" if ev.get('start_time') else ""
                        body = f"{ev['title']}{time_str}"
                        title = "Huddle: Tomorrow"
                    else:
                        body = ', '.join(e['title'] for e in person_events[:4])
                        if len(person_events) > 4:
                            body += f' +{len(person_events) - 4} more'
                        title = f"Huddle: {len(person_events)} events tomorrow"

                    send_push_to_person(person, title, body, "calendar-reminder", household_id=hid, module="calendar")
                    logger.info("  Calendar reminder to %s: %s", person, body)

    # ── Sunday evening: Weekly stats summary ──
    if now.weekday() == 6 and evening_time <= current_time < evening_time + 15 and last_weekly_dates.get(hid) != today_str and all_settings.get("notify_weekly", "true") == "true":
        last_weekly_dates[hid] = today_str
        logger.info("Sending weekly summary notifications for household %d...", hid)

        with get_db() as conn:
            monday = (now - timedelta(days=6)).date()
            stats = {}
            for person in people:
                count = conn.execute("""
                    SELECT COUNT(*) FROM completions c
                    JOIN chores ch ON c.chore_id = ch.id
                    WHERE c.completed_by = ?
                    AND c.completed_at >= ?
                    AND c.completed_by != 'SKIPPED'
                    AND ch.household_id = ?
                """, (person, monday.isoformat(), hid)).fetchone()[0]
                stats[person] = count

            total = sum(stats.values())
            if total > 0:
                stats_text = ', '.join(f"{p}: {c}" for p, c in stats.items())
                for person in people:
                    send_push_to_person(
                        person,
                        f"Huddle: Weekly summary - {total} chores done!",
                        stats_text,
                        "weekly-summary",
                        household_id=hid,
                        module="chores",
                    )
                logger.info("  Weekly summary sent for household %d: %s", hid, stats_text)
