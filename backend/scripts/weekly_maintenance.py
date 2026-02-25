#!/usr/bin/env python3
"""
Huddle Weekly Maintenance — database vacuum, push cleanup, log rotation check.

Runs once a week (Sunday 3 AM AEDT). Handles:
1. Database VACUUM + ANALYZE for query planner and space reclaim
2. Stale push subscription bulk cleanup (410/404 failures)
3. Log rotation safety check (truncate if rotation handler failed)

Crontab: 0 16 * * 0 /home/keiran/huddle/backend/venv/bin/python /home/keiran/huddle/backend/scripts/weekly_maintenance.py
"""

import json
import logging
import logging.handlers
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).parent.parent
DB_FILE = PROJECT_DIR / "chores.db"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Logger
logger = logging.getLogger("huddle.maintenance")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "maintenance.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logger.addHandler(console)


# ── 1. Database VACUUM + ANALYZE ──────────────────────────────────────────

def vacuum_database():
    """Run VACUUM and ANALYZE on the database to reclaim space and update stats."""
    try:
        if not DB_FILE.exists():
            logger.error("Database not found: %s", DB_FILE)
            return

        size_before = DB_FILE.stat().st_size
        conn = sqlite3.connect(str(DB_FILE))

        # ANALYZE updates the sqlite_stat1 table for the query planner
        conn.execute("ANALYZE")
        logger.info("ANALYZE complete")

        # VACUUM rebuilds the database file, reclaiming free pages
        conn.execute("VACUUM")
        conn.close()

        size_after = DB_FILE.stat().st_size
        saved = size_before - size_after
        logger.info(
            "VACUUM complete: %.1f KB -> %.1f KB (reclaimed %.1f KB)",
            size_before / 1024, size_after / 1024, saved / 1024,
        )
    except Exception as e:
        logger.error("Database vacuum failed: %s", e, exc_info=True)


# ── 2. Stale push subscription cleanup ───────────────────────────────────

def cleanup_push_subscriptions():
    """Test all push subscriptions and remove ones that return 410/404."""
    try:
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            logger.info("pywebpush not installed, skipping push cleanup")
            return

        # Load VAPID keys
        vapid_file = PROJECT_DIR / "vapid_keys.json"
        if not vapid_file.exists():
            logger.info("No VAPID keys, skipping push cleanup")
            return

        with open(vapid_file) as f:
            vapid = json.load(f)
        private_key = vapid["private_key"]

        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, person, subscription, household_id FROM push_subscriptions").fetchall()
        logger.info("Checking %d push subscriptions...", len(rows))

        expired_ids = []
        for row in rows:
            try:
                sub_info = json.loads(row["subscription"])
                endpoint = sub_info.get("endpoint", "")
                parsed = urlparse(endpoint)
                aud = f"{parsed.scheme}://{parsed.netloc}"

                # Send a silent notification (empty payload triggers nothing visible)
                webpush(
                    subscription_info=sub_info,
                    data=json.dumps({"title": "", "body": "", "tag": "health-check", "silent": True}),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": "mailto:chores@example.com", "aud": aud},
                    ttl=0,  # Don't queue if device is offline
                )
            except WebPushException as e:
                import re
                resp = getattr(e, 'response', None)
                status = getattr(resp, 'status_code', None) or getattr(resp, 'status', None)
                if status is None:
                    m = re.search(r'\b(4\d\d|5\d\d)\b', str(e))
                    if m:
                        status = int(m.group(1))
                if status in (410, 404):
                    expired_ids.append((row["id"], row["person"], row["household_id"]))
            except Exception:
                pass  # Network errors are fine, subscription may still be valid

        if expired_ids:
            for sub_id, person, hid in expired_ids:
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
            conn.commit()
            logger.info("Removed %d expired push subscriptions", len(expired_ids))
            # Log details
            for sub_id, person, hid in expired_ids:
                logger.info("  Removed subscription %d for %s (household %d)", sub_id, person, hid)
        else:
            logger.info("All push subscriptions are valid")

        conn.close()
    except Exception as e:
        logger.error("Push subscription cleanup failed: %s", e, exc_info=True)


# ── 3. Log rotation safety check ─────────────────────────────────────────

MAX_LOG_SIZE = 50 * 1024 * 1024  # 50 MB hard limit

def check_log_rotation():
    """Ensure log files haven't grown beyond safe limits."""
    log_files = {
        "huddle.log": PROJECT_DIR / "logs" / "huddle.log",
        "watchdog.log": PROJECT_DIR / "logs" / "watchdog.log",
        "backup.log": PROJECT_DIR / "logs" / "backup.log",
        "autofix.log": PROJECT_DIR / "logs" / "autofix.log",
        "maintenance.log": PROJECT_DIR / "logs" / "maintenance.log",
    }

    for name, path in log_files.items():
        try:
            if not path.exists():
                continue

            size = path.stat().st_size
            size_mb = size / (1024 * 1024)

            if size > MAX_LOG_SIZE:
                # Truncate to last 10,000 lines to preserve recent context
                logger.warning(
                    "%s is %.1f MB (limit %.1f MB), truncating...",
                    name, size_mb, MAX_LOG_SIZE / (1024 * 1024),
                )
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    keep = lines[-10000:] if len(lines) > 10000 else lines
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(f"[Log truncated by maintenance at {__import__('datetime').datetime.now().isoformat()}]\n")
                        f.writelines(keep)
                    new_size = path.stat().st_size / (1024 * 1024)
                    logger.info("%s truncated: %.1f MB -> %.1f MB", name, size_mb, new_size)
                except Exception as e:
                    logger.error("Failed to truncate %s: %s", name, e)
            else:
                logger.info("%s: %.1f MB (OK)", name, size_mb)
        except Exception as e:
            logger.error("Log check failed for %s: %s", name, e)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Weekly maintenance started ===")

    vacuum_database()
    cleanup_push_subscriptions()
    check_log_rotation()

    logger.info("=== Weekly maintenance complete ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            logger.critical("Maintenance fatal error: %s", e, exc_info=True)
        except Exception:
            print(f"MAINTENANCE FATAL: {e}", file=sys.stderr)
