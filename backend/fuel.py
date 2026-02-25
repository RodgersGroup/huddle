import logging
import sqlite3
from pathlib import Path
from settings import get_setting
from fastapi import APIRouter, HTTPException
from datetime import datetime
import json
import time
import re

logger = logging.getLogger("huddle")

BASE_DIR = Path(__file__).parent

router = APIRouter()

# NSW Fuel API Configuration
FUEL_API_URL = "https://api.onegov.nsw.gov.au/FuelCheckApp/v1/fuel/prices/nearby"
FUEL_RADIUS = 5
FUEL_TYPES = ["E10", "U91", "P95", "P98"]

# Fuel cache (refresh every 10 minutes for more current data)
fuel_cache = {"data": None, "timestamp": 0}
FUEL_CACHE_TTL = 600  # 10 minutes (was 30)
FUEL_CACHE_FILE = str(BASE_DIR / "fuel_display.txt")

def parse_fuel_cache_file():
    """Parse cached fuel data from fuel_display.txt as fallback."""
    try:
        if not Path(FUEL_CACHE_FILE).exists():
            logger.debug("Fuel cache file not found, skipping: %s", FUEL_CACHE_FILE)
            return None

        with open(FUEL_CACHE_FILE, 'r') as f:
            content = f.read()

        results = {}
        current_type = None

        for line in content.split('\n'):
            line = line.strip()
            if line in ['E10', 'U91', 'P95', 'P98']:
                current_type = line
                results[current_type] = []
            elif current_type and 'c' in line and 'km' in line:
                match = re.match(r'(\d+\.?\d*)c\s+(.+)\s+(\d+\.?\d*)km$', line)
                if match:
                    results[current_type].append({
                        "name": match.group(2).strip(),
                        "price": float(match.group(1)),
                        "distance": float(match.group(3))
                    })

        time_match = re.search(r'Updated:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})', content)
        updated = time_match.group(1) if time_match else "cached"

        if not any(results.values()):
            return None

        return {"fuel": results, "updated": updated, "location": get_setting("location_name", "Shortland 2307", household_id=1), "cached": True}
    except Exception as e:
        logger.error("Failed to parse fuel cache file: %s", e, exc_info=True)
        return None


@router.get("/api/fuel")
async def get_fuel():
    """Fetch NSW fuel prices - uses cache file updated by cron, with API fallback."""
    import urllib.request
    import os

    global fuel_cache

    try:
        now = time.time()

        if fuel_cache["data"] and (now - fuel_cache["timestamp"]) < FUEL_CACHE_TTL:
            return fuel_cache["data"]

        cache_data = parse_fuel_cache_file()
        if cache_data:
            try:
                mtime = os.path.getmtime(FUEL_CACHE_FILE)
                age_minutes = int((now - mtime) / 60)
                if age_minutes > 120:
                    cache_data["stale"] = True
                    cache_data["age_minutes"] = age_minutes
            except OSError as e:
                logger.debug("Could not stat fuel cache file: %s", e)

            fuel_cache["data"] = cache_data
            fuel_cache["timestamp"] = now
            return cache_data

        from config import FUEL_API_KEY
        fuel_api_key = FUEL_API_KEY or get_setting("fuel_api_key", "", household_id=1)
        fuel_lat = float(get_setting("location_lat", "-32.879", household_id=1))
        fuel_lon = float(get_setting("location_lon", "151.691", household_id=1))
        location_name = get_setting("location_name", "Shortland 2307", household_id=1)
        results = {}
        api_success = False

        for fuel_type in FUEL_TYPES:
            try:
                headers = {
                    "apikey": fuel_api_key,
                    "Content-Type": "application/json",
                    "requesttimestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                }
                payload = json.dumps({
                    "fueltype": fuel_type,
                    "latitude": fuel_lat,
                    "longitude": fuel_lon,
                    "radius": FUEL_RADIUS,
                    "sortby": "Price",
                    "sortascending": "true"
                }).encode('utf-8')

                req = urllib.request.Request(
                    FUEL_API_URL,
                    data=payload,
                    headers=headers,
                    method='POST'
                )

                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode())

                    stations = {s["code"]: s for s in data.get("stations", [])}
                    prices = data.get("prices", [])

                    fuel_results = []
                    for price in prices:
                        code = price.get("stationcode")
                        if code in stations:
                            s = stations[code]
                            fuel_results.append({
                                "name": s.get("name", "Unknown"),
                                "price": price.get("price", 0),
                                "distance": s.get("location", {}).get("distance", 0),
                            })

                    fuel_results.sort(key=lambda x: x["price"])
                    results[fuel_type] = fuel_results[:3]
                    if fuel_results:
                        api_success = True

            except Exception as e:
                logger.error("Fuel API error for %s: %s", fuel_type, e, exc_info=True)
                results[fuel_type] = []

        if api_success:
            response_data = {
                "fuel": results,
                "updated": datetime.now().isoformat(),
                "location": location_name
            }
            fuel_cache["data"] = response_data
            fuel_cache["timestamp"] = now
            return response_data

        return {
            "fuel": {"E10": [], "P95": [], "P98": []},
            "updated": None,
            "location": location_name,
            "error": "No fuel data available"
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_fuel: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_fuel: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
