#!/usr/bin/env python3
"""
Huddle Watchdog - Periodic health checker.

Checks backend health, disk usage, database integrity, and memory.
Logs all results to logs/watchdog.log.

Add to crontab with: crontab -e
# */5 * * * * /home/keiran/huddle/backend/venv/bin/python /home/keiran/huddle/backend/scripts/watchdog.py
"""

import logging
import logging.handlers
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
DB_FILE = PROJECT_DIR / "chores.db"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Set up watchdog logger (separate from main app logger)
logger = logging.getLogger("huddle.watchdog")
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "watchdog.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def check_backend():
    """Check if the backend is responding."""
    try:
        req = urllib.request.Request("http://localhost:8001/api/health/ping")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                logger.info("Backend: healthy (200 OK)")
                return True
            else:
                logger.error("Backend: unhealthy (HTTP %d)", resp.status)
                return False
    except Exception as e:
        logger.error("Backend: unreachable (%s)", e)
        return False


def restart_backend():
    """Attempt to restart the backend service."""
    logger.warning("Attempting to restart chores-kiosk service...")
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "chores-kiosk"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Service restart: success")
        else:
            logger.error("Service restart: failed (exit %d): %s", result.returncode, result.stderr.strip())
    except Exception as e:
        logger.error("Service restart: error (%s)", e)


def check_disk():
    """Check disk usage."""
    try:
        usage = shutil.disk_usage("/")
        percent = (usage.used / usage.total) * 100
        free_gb = usage.free / (1024 ** 3)
        if percent > 85:
            logger.warning("Disk usage: %.1f%% (%.1f GB free) — above 85%% threshold", percent, free_gb)
        else:
            logger.info("Disk usage: %.1f%% (%.1f GB free)", percent, free_gb)
    except Exception as e:
        logger.error("Disk check failed: %s", e)


def check_database():
    """Check database file exists and is not corrupt."""
    try:
        if not DB_FILE.exists():
            logger.error("Database: file not found at %s", DB_FILE)
            return

        size_mb = DB_FILE.stat().st_size / (1024 * 1024)
        conn = sqlite3.connect(str(DB_FILE))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()

        if result and result[0] == "ok":
            logger.info("Database: healthy (%.1f MB, integrity OK)", size_mb)
        else:
            logger.error("Database: integrity check failed: %s", result)
    except Exception as e:
        logger.error("Database check failed: %s", e)


def check_memory():
    """Check available memory."""
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])

        available_mb = meminfo.get("MemAvailable", 0) / 1024
        total_mb = meminfo.get("MemTotal", 0) / 1024

        if available_mb < 100:
            logger.warning("Memory: %.0f MB available of %.0f MB — below 100 MB threshold", available_mb, total_mb)
        else:
            logger.info("Memory: %.0f MB available of %.0f MB", available_mb, total_mb)
    except Exception as e:
        logger.error("Memory check failed: %s", e)


def main():
    logger.info("=== Watchdog check started ===")

    try:
        backend_ok = check_backend()
        if not backend_ok:
            restart_backend()
    except Exception as e:
        logger.error("Backend check crashed: %s", e)

    try:
        check_disk()
    except Exception as e:
        logger.error("Disk check crashed: %s", e)

    try:
        check_database()
    except Exception as e:
        logger.error("Database check crashed: %s", e)

    try:
        check_memory()
    except Exception as e:
        logger.error("Memory check crashed: %s", e)

    logger.info("=== Watchdog check complete ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # The watchdog itself must never crash without logging
        try:
            logger.critical("Watchdog fatal error: %s", e, exc_info=True)
        except Exception:
            print(f"WATCHDOG FATAL: {e}", file=sys.stderr)
