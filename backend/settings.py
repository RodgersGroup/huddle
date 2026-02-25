import json
import logging
import threading
import time

from db import get_db

logger = logging.getLogger("huddle")

# Default settings values (generic — no household-specific data)
SETTINGS_DEFAULTS = {
    "household_members": "[]",
    "member_colors": "{}",
    "location_lat": "-33.8688",
    "location_lon": "151.2093",
    "location_name": "",
    "timezone": "Australia/Sydney",
    "fuel_enabled": "false",
    "screen_on_time": "06:30",
    "screen_off_time": "21:00",
    "notify_morning": "07:30",
    "notify_afternoon": "16:00",
    "notify_evening": "19:00",
    "notify_chores": "true",
    "notify_routines": "true",
    "notify_selfcare": "true",
    "notify_meals": "true",
    "notify_calendar": "true",
    "notify_weekly": "true",
    "notify_noticeboard": "true",
}

# ---------------------------------------------------------------------------
# In-memory cache with 30-second TTL.  Eliminates 3-10 redundant DB reads
# per request for settings, people, and member colours.
# ---------------------------------------------------------------------------
_CACHE_TTL = 30  # seconds
_cache = {}      # key -> (value, expiry_timestamp)
_cache_lock = threading.Lock()


def _cache_get(key):
    """Return cached value or None if missing/expired."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
    return None


def _cache_set(key, value):
    """Store a value in the cache with TTL."""
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + _CACHE_TTL)


def invalidate_settings_cache(household_id: int = None):
    """Clear cached settings.  Called after save_setting() and by the
    settings API router after bulk updates."""
    with _cache_lock:
        if household_id is None:
            _cache.clear()
        else:
            keys_to_remove = [k for k in _cache if k[0] == household_id or (isinstance(k, tuple) and len(k) >= 1 and k[0] == household_id)]
            for k in keys_to_remove:
                del _cache[k]


def _resolve_household_id(household_id):
    """Resolve household_id, raising ValueError if not provided."""
    if household_id is None:
        raise ValueError(
            "household_id is required but was None. "
            "All settings/people calls must provide an explicit household_id."
        )
    return household_id


def get_setting(key: str, default=None, household_id: int = None) -> str:
    """Read a single setting from the database, with fallback to defaults."""
    household_id = _resolve_household_id(household_id)
    if default is None:
        default = SETTINGS_DEFAULTS.get(key)

    cache_key = (household_id, "setting", key)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ? AND household_id = ?",
            (key, household_id)
        ).fetchone()
        if row:
            _cache_set(cache_key, row["value"])
            return row["value"]
    return default


def save_setting(key: str, value: str, household_id: int = None):
    """Write a single setting to the database."""
    household_id = _resolve_household_id(household_id)
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
            (key, str(value), household_id)
        )
        conn.commit()
    invalidate_settings_cache(household_id)


def get_all_settings(household_id: int = None) -> dict:
    """Get all settings as a dict, filling in defaults for missing keys."""
    household_id = _resolve_household_id(household_id)

    cache_key = (household_id, "all_settings")
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached)  # return a copy to prevent mutation

    result = dict(SETTINGS_DEFAULTS)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE household_id = ?",
            (household_id,)
        ).fetchall()
        for row in rows:
            result[row["key"]] = row["value"]
    _cache_set(cache_key, result)
    return result


def get_people(household_id: int = None) -> list:
    """Get household member names. Reads from household_members table,
    falling back to settings key for legacy compatibility."""
    household_id = _resolve_household_id(household_id)

    cache_key = (household_id, "people")
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)  # return a copy

    # Primary source: household_members table
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT display_name FROM household_members WHERE household_id = ? ORDER BY display_name",
                (household_id,)
            ).fetchall()
            if rows:
                result = [r["display_name"] for r in rows]
                _cache_set(cache_key, result)
                return result
    except Exception as e:
        logger.error("Failed to query household members for household %s: %s", household_id, e)
    # Fallback: legacy settings key
    raw = get_setting("household_members", household_id=household_id)
    try:
        result = json.loads(raw)
        _cache_set(cache_key, result)
        return result
    except (json.JSONDecodeError, TypeError):
        return []


def get_member_colors(household_id: int = None) -> dict:
    """Get per-member accent colors. Reads from household_members table,
    falling back to settings key for legacy compatibility."""
    household_id = _resolve_household_id(household_id)

    cache_key = (household_id, "member_colors")
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached)  # return a copy

    # Primary source: household_members table
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT display_name, color FROM household_members WHERE household_id = ?",
                (household_id,)
            ).fetchall()
            if rows:
                result = {r["display_name"]: r["color"] for r in rows}
                _cache_set(cache_key, result)
                return result
    except Exception as e:
        logger.error("Failed to query member colors for household %s: %s", household_id, e)
    # Fallback: legacy settings key
    raw = get_setting("member_colors", household_id=household_id)
    try:
        result = json.loads(raw)
        _cache_set(cache_key, result)
        return result
    except (json.JSONDecodeError, TypeError):
        return {}


def get_calendar_colors(household_id: int = None) -> dict:
    """Build CALENDAR_COLORS dynamically from member settings."""
    people = get_people(household_id=household_id)
    member_colors = get_member_colors(household_id=household_id)

    colors = {
        "guests": "#ff9800",
        "everyone": "#7e57c2",
        "one_off": "#2196f3",
    }

    for p in people:
        colors[p.lower()] = member_colors.get(p, "#00bcd4")

    combo_palette = ["#ff7043", "#ab47bc", "#ffeb3b", "#26a69a", "#ef5350", "#66bb6a"]
    combo_idx = 0
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            key = "_".join(sorted([people[i].lower(), people[j].lower()]))
            colors[key] = combo_palette[combo_idx % len(combo_palette)]
            combo_idx += 1

    return colors


def get_participant_color(participants: list, household_id: int = None) -> str:
    """Determine color based on participants list."""
    colors = get_calendar_colors(household_id=household_id)
    people = get_people(household_id=household_id)

    if not participants:
        return colors["guests"]

    p_set = set(p.lower() for p in participants)

    if "guests" in p_set:
        return colors["guests"]

    if len(p_set) == 1:
        person = list(p_set)[0]
        return colors.get(person, colors["guests"])

    if len(p_set) == 2:
        key = "_".join(sorted(p_set))
        return colors.get(key, colors["guests"])

    if p_set == set(p.lower() for p in people):
        return colors["everyone"]

    return colors["guests"]


def get_participant_label(participants: list, household_id: int = None) -> str:
    """Get display label for participants."""
    if not participants:
        return "Guests"

    p_set = set(p.lower() for p in participants)
    people = get_people(household_id=household_id)

    if "guests" in p_set:
        return "Guests"

    if len(p_set) == 1:
        return list(participants)[0]

    if p_set == set(p.lower() for p in people):
        return "Everyone"

    if len(p_set) == 2:
        names = [p for p in participants if p.lower() in p_set]
        return " & ".join(names)

    return ", ".join(participants)
