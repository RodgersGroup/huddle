#!/usr/bin/env python3
"""
NSW Fuel Watch App - Kiosk Display
Finds the cheapest fuel prices near Shortland (2307)
"""

import requests
from datetime import datetime

# API Configuration
API_KEY = "Tz7GETUw0DXcEOLrHoPEkoqTHZQyVrzx"
BASE_URL = "https://api.onegov.nsw.gov.au"
NEARBY_ENDPOINT = "/FuelCheckApp/v1/fuel/prices/nearby"

# Location: Postcode 2307 (Shortland, NSW)
LATITUDE = -32.879
LONGITUDE = 151.691
RADIUS = 5

FUEL_TYPES = ["E10", "U91", "P95", "P98"]


def get_fuel_prices(fuel_type):
    """Get fuel prices for a specific fuel type."""
    headers = {
        "apikey": API_KEY,
        "Content-Type": "application/json",
        "requesttimestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }
    payload = {
        "fueltype": fuel_type,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "radius": RADIUS,
        "sortby": "Price",
        "sortascending": "true"
    }

    try:
        response = requests.post(f"{BASE_URL}{NEARBY_ENDPOINT}", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        stations = {s["code"]: s for s in data.get("stations", [])}
        prices = data.get("prices", [])

        results = []
        for price in prices:
            code = price.get("stationcode")
            if code in stations:
                s = stations[code]
                results.append({
                    "name": s.get("name", "Unknown"),
                    "price": price.get("price", 0),
                    "dist": s.get("location", {}).get("distance", 0),
                })

        results.sort(key=lambda x: x["price"])
        return results[:3]
    except:
        return []


def truncate(text, length):
    """Truncate text to fit display."""
    return text[:length-2] + ".." if len(text) > length else text


def main():
    print("\033[2J\033[H", end="")  # Clear screen

    now = datetime.now().strftime("%H:%M")
    print(f"{'='*40}")
    print(f"  FUEL PRICES - Shortland 2307  {now}")
    print(f"{'='*40}")

    for fuel_type in FUEL_TYPES:
        results = get_fuel_prices(fuel_type)

        print(f"\n  {fuel_type}")
        print(f"  {'-'*36}")

        if not results:
            print("  No data available")
            continue

        for r in results:
            name = truncate(r["name"], 22)
            price = r["price"]
            dist = r["dist"]
            print(f"  {price:5.1f}c  {name:<22} {dist:.1f}km")

    print(f"\n{'='*40}")
    print(f"  Updated: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*40}\n")


if __name__ == "__main__":
    main()
