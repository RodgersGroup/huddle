import logging
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime
import json
import time
import os
from pathlib import Path
from db import get_db
from settings import get_setting, save_setting, get_all_settings, get_people, get_member_colors, get_calendar_colors
from websocket import manager
from fuel import fuel_cache, FUEL_CACHE_TTL, FUEL_CACHE_FILE

logger = logging.getLogger("huddle")

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent


# ============================================================================
# SETTINGS API
# ============================================================================

@router.get("/api/settings")
def api_get_settings(tenant: TenantContext = Depends(get_current_user)):
    """Get all settings as key/value object."""
    household_id = tenant.household_id
    try:
        return get_all_settings(household_id=household_id)
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in api_get_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in api_get_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/settings")
async def api_put_settings(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Bulk update settings."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        if not isinstance(data, dict):
            logger.warning("Validation failed in api_put_settings: %s", "Request body must be a JSON object")
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        for key, value in data.items():
            if not isinstance(key, str) or not key.strip():
                logger.warning("Validation failed in api_put_settings: %s", "Setting keys must be non-empty strings")
                raise HTTPException(status_code=400, detail="Setting keys must be non-empty strings")
            if len(key) > 200:
                logger.warning("Validation failed in api_put_settings: %s", f"Setting key '{key[:50]}...' is too long (max 200 characters)")
                raise HTTPException(status_code=400, detail=f"Setting key is too long (max 200 characters)")
            if isinstance(value, str) and len(value) > 1000:
                logger.warning("Validation failed in api_put_settings: %s", f"Value for key '{key}' is too long (max 1000 characters)")
                raise HTTPException(status_code=400, detail=f"Value for key '{key}' is too long (max 1000 characters)")
        # --- End validation ---

        for key, value in data.items():
            save_setting(key, value, household_id=household_id)
        await manager.broadcast({"type": "settings_updated"}, household_id=household_id)
        return {"message": f"Updated {len(data)} settings"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in api_put_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in api_put_settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/settings/{key}")
def api_get_setting(key: str, tenant: TenantContext = Depends(get_current_user)):
    """Get a single setting."""
    household_id = tenant.household_id
    try:
        value = get_setting(key, household_id=household_id)
        if value is None:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
        return {"key": key, "value": value}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in api_get_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in api_get_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/settings/{key}")
async def api_put_setting(key: str, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a single setting."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        value = data.get("value")
        if value is None:
            logger.warning("Validation failed in api_put_setting: %s", "'value' field required")
            raise HTTPException(status_code=400, detail="'value' field required")

        # --- Input validation ---
        if isinstance(value, str) and len(value) > 1000:
            logger.warning("Validation failed in api_put_setting: %s", "Value is too long (max 1000 characters)")
            raise HTTPException(status_code=400, detail="Value is too long (max 1000 characters)")
        # --- End validation ---

        save_setting(key, value, household_id=household_id)
        await manager.broadcast({"type": "settings_updated", "key": key}, household_id=household_id)
        return {"key": key, "value": value}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in api_put_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in api_put_setting: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# PEOPLE API - Dynamic household member data for templates
# ============================================================================

@router.get("/api/people")
def api_get_people(tenant: TenantContext = Depends(get_current_user)):
    """Get household members with their colors for dynamic UI rendering."""
    household_id = tenant.household_id
    try:
        people = get_people(household_id=household_id)
        colors = get_member_colors(household_id=household_id)
        calendar_colors = get_calendar_colors(household_id=household_id)

        # Look up avatar URLs from household_members
        from auth import get_avatar_url
        from db import get_db
        avatar_map = {}
        with get_db() as conn:
            rows = conn.execute(
                "SELECT hm.display_name, hm.user_id FROM household_members hm WHERE hm.household_id = ?",
                (household_id,)
            ).fetchall()
            for row in rows:
                avatar_map[row["display_name"]] = get_avatar_url(row["user_id"])

        # Build role and member_id maps
        role_map = {}
        member_id_map = {}
        with get_db() as conn:
            member_rows = conn.execute(
                "SELECT id, display_name, role FROM household_members WHERE household_id = ?",
                (household_id,)
            ).fetchall()
            for mr in member_rows:
                role_map[mr["display_name"]] = mr["role"]
                member_id_map[mr["display_name"]] = mr["id"]

        members = []
        for person in people:
            members.append({
                "name": person,
                "color": colors.get(person, "#4ecdc4"),
                "key": person.lower(),
                "avatar_url": avatar_map.get(person, ""),
                "role": role_map.get(person, "member"),
                "member_id": member_id_map.get(person),
            })

        # Build combo presets for 2-person combinations
        combos = []
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                a, b = people[i], people[j]
                combo_key = "_".join(sorted([a.lower(), b.lower()]))
                combos.append({
                    "key": combo_key,
                    "names": [a, b],
                    "label": f"{a} & {b}",
                    "color": calendar_colors.get(combo_key, "#ff9800"),
                })

        return {
            "people": members,
            "combos": combos,
            "everyone_color": calendar_colors.get("everyone", "#7e57c2"),
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in api_get_people: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in api_get_people: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# QUOTE API
# ============================================================================

# Quote API with hourly caching
quote_cache = {"quote": None, "author": None, "hour": None}

# Fallback quotes for when API fails
FALLBACK_QUOTES = [
    {"quote": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
    {"quote": "A clean house is a sign of a wasted life... just kidding, clean up!", "author": "Unknown"},
    {"quote": "Teamwork makes the dream work.", "author": "John C. Maxwell"},
    {"quote": "Small daily improvements lead to staggering long-term results.", "author": "Robin Sharma"},
    {"quote": "The best time to plant a tree was 20 years ago. The second best time is now.", "author": "Chinese Proverb"},
    {"quote": "Done is better than perfect.", "author": "Sheryl Sandberg"},
    {"quote": "A journey of a thousand miles begins with a single step.", "author": "Lao Tzu"},
    {"quote": "Success is the sum of small efforts repeated day in and day out.", "author": "Robert Collier"},
]


@router.get("/api/quote")
def get_quote():
    """Get an inspirational quote, cached per hour (Australian Eastern time)."""
    import urllib.request
    import random

    global quote_cache

    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        # Get current hour in Australian Eastern time
        aest = ZoneInfo(get_setting("timezone", "Australia/Sydney", household_id=1))
        current_hour = datetime.now(aest).strftime('%Y-%m-%d-%H')

        # Return cached quote if still within the same hour
        if quote_cache["hour"] == current_hour and quote_cache["quote"]:
            return {
                "quote": quote_cache["quote"],
                "author": quote_cache["author"],
                "cached": True
            }

        # Try to fetch a new quote from ZenQuotes API
        try:
            req = urllib.request.Request(
                "https://zenquotes.io/api/today",
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if data and len(data) > 0:
                    quote_data = data[0]
                    quote_cache["quote"] = quote_data.get("q", "")
                    quote_cache["author"] = quote_data.get("a", "Unknown")
                    quote_cache["hour"] = current_hour
                    return {
                        "quote": quote_cache["quote"],
                        "author": quote_cache["author"],
                        "cached": False
                    }
        except Exception as e:
            logger.error("Quote API error: %s", e)

        # Fallback: use a random quote from our list based on the hour
        hour_num = int(datetime.now(aest).strftime('%H'))
        fallback = FALLBACK_QUOTES[hour_num % len(FALLBACK_QUOTES)]
        quote_cache["quote"] = fallback["quote"]
        quote_cache["author"] = fallback["author"]
        quote_cache["hour"] = current_hour

        return {
            "quote": quote_cache["quote"],
            "author": quote_cache["author"],
            "fallback": True
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in get_quote: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# WEATHER API
# ============================================================================

@router.get("/api/weather")
def get_weather(tenant: TenantContext = Depends(get_current_user)):
    """Proxy weather API to avoid CORS/SSL issues on Pi."""
    import urllib.request
    import json

    household_id = tenant.household_id
    try:
        lat = float(get_setting("location_lat", "-32.879", household_id=household_id))
        lon = float(get_setting("location_lon", "151.691", household_id=household_id))
        tz = get_setting("timezone", "Australia/Sydney", household_id=household_id)
        location_name = get_setting("location_name", "", household_id=household_id)
        # Expanded weather data: wind, UV, sunrise/sunset, cloud cover, 6-day forecast
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,weather_code,relative_humidity_2m,"
            f"wind_speed_10m,wind_direction_10m,cloud_cover,uv_index"
            f"&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,"
            f"sunrise,sunset,uv_index_max,wind_speed_10m_max,wind_direction_10m_dominant"
            f"&timezone={tz.replace('/', '%2F')}"
            f"&forecast_days=7"
        )

        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            data["location"] = location_name
            return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Weather fetch error: %s", e, exc_info=True)
        return {"error": str(e)}


# ============================================================================
# SERVICE STATUS & KIOSK CONTROL
# ============================================================================

@router.get("/api/service/status")
def get_service_status():
    """Get status of all services including fuel cache."""
    try:
        now = time.time()
        status = {
            "timestamp": datetime.now().isoformat(),
            "services": {}
        }

        # Fuel cache status
        fuel_age = now - fuel_cache["timestamp"] if fuel_cache["timestamp"] > 0 else None
        status["services"]["fuel"] = {
            "cached": fuel_cache["data"] is not None,
            "cache_age_seconds": int(fuel_age) if fuel_age else None,
            "cache_fresh": fuel_age is not None and fuel_age < FUEL_CACHE_TTL,
            "ttl_seconds": FUEL_CACHE_TTL
        }

        # Fuel cache file status
        try:
            mtime = os.path.getmtime(FUEL_CACHE_FILE)
            file_age = now - mtime
            status["services"]["fuel_file"] = {
                "exists": True,
                "age_seconds": int(file_age),
                "fresh": file_age < 7200  # Less than 2 hours old
            }
        except OSError as e:
            logger.debug("Fuel cache file not found or inaccessible: %s", e)
            status["services"]["fuel_file"] = {"exists": False}

        return status
    except HTTPException:
        raise
    except Exception as e:
        logger.critical("Unexpected error in get_service_status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/kiosk/refresh")
async def refresh_kiosk(tenant: TenantContext = Depends(get_current_user)):
    """Tell connected kiosk browsers for this household to reload via WebSocket."""
    try:
        await manager.broadcast({"type": "refresh"}, household_id=tenant.household_id)
        return {"success": True, "message": "Refresh signal sent to kiosk"}
    except Exception as e:
        logger.critical("Unexpected error in refresh_kiosk: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/screen/on")
async def screen_on(tenant: TenantContext = Depends(get_current_user)):
    """Tell the kiosk Pi to turn its screen on via WebSocket."""
    try:
        await manager.broadcast({"type": "screen_on"}, household_id=tenant.household_id)
        return {"success": True, "message": "Screen on signal sent to kiosk"}
    except Exception as e:
        logger.critical("Unexpected error in screen_on: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/screen/off")
async def screen_off(tenant: TenantContext = Depends(get_current_user)):
    """Tell the kiosk Pi to turn its screen off via WebSocket."""
    try:
        await manager.broadcast({"type": "screen_off"}, household_id=tenant.household_id)
        return {"success": True, "message": "Screen off signal sent to kiosk"}
    except Exception as e:
        logger.critical("Unexpected error in screen_off: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
