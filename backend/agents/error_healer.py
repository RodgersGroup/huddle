"""Error Healer Agent — monitors and auto-fixes known error conditions."""
import logging
import os
from datetime import datetime
from pathlib import Path
from db import get_db
from agents.base import should_run, log_action, alert_managers

logger = logging.getLogger("huddle.agents")

AGENT = "error_healer"
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_FILE = LOG_DIR / "huddle.log"


def run_system_checks():
    """Run system-wide (non-household) healing checks."""
    _heal_expired_push_subscriptions()
    _heal_orphaned_completions()
    _heal_orphaned_routine_completions()
    _heal_stuck_pairing_codes()
    _check_log_file_growth()


def _heal_expired_push_subscriptions():
    """Remove excess push subscriptions (>5 per person per household, keep newest 3)."""
    if not should_run(AGENT, "expired_push", 3600):
        return
    try:
        with get_db() as conn:
            dupes = conn.execute("""
                SELECT person, household_id, COUNT(*) as cnt
                FROM push_subscriptions
                GROUP BY person, household_id
                HAVING cnt > 5
            """).fetchall()
            cleaned = 0
            for dupe in dupes:
                to_delete = conn.execute("""
                    SELECT id FROM push_subscriptions
                    WHERE person = ? AND household_id = ?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET 3
                """, (dupe["person"], dupe["household_id"])).fetchall()
                for row in to_delete:
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
                    cleaned += 1
            if cleaned:
                conn.commit()
                log_action(AGENT, "expired_push_cleanup",
                           f"Cleaned {cleaned} excess push subscriptions",
                           auto_fixed=True)
    except Exception as e:
        logger.warning("Error healer push cleanup failed: %s", e)


def _heal_orphaned_completions():
    """Remove completion records that reference deleted chores."""
    if not should_run(AGENT, "orphan_completions", 21600):
        return
    try:
        with get_db() as conn:
            orphans = conn.execute("""
                SELECT c.id FROM completions c
                LEFT JOIN chores ch ON c.chore_id = ch.id
                WHERE ch.id IS NULL
            """).fetchall()
            if orphans:
                ids = [r["id"] for r in orphans]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM completions WHERE id IN ({placeholders})", ids)
                conn.commit()
                log_action(AGENT, "orphan_completions",
                           f"Removed {len(orphans)} orphaned completion records",
                           auto_fixed=True)
    except Exception as e:
        logger.warning("Error healer orphan completions cleanup failed: %s", e)


def _heal_orphaned_routine_completions():
    """Remove routine completion records for deleted routines or items."""
    if not should_run(AGENT, "orphan_routine_completions", 21600):
        return
    try:
        with get_db() as conn:
            orphans_routine = conn.execute("""
                SELECT rc.id FROM routine_completions rc
                LEFT JOIN routines r ON rc.routine_id = r.id
                WHERE r.id IS NULL
            """).fetchall()
            orphans_item = conn.execute("""
                SELECT rc.id FROM routine_completions rc
                LEFT JOIN routine_items ri ON rc.item_id = ri.id
                WHERE ri.id IS NULL
            """).fetchall()
            all_orphans = set(r["id"] for r in orphans_routine) | set(r["id"] for r in orphans_item)
            if all_orphans:
                placeholders = ",".join("?" for _ in all_orphans)
                conn.execute(f"DELETE FROM routine_completions WHERE id IN ({placeholders})", list(all_orphans))
                conn.commit()
                log_action(AGENT, "orphan_routine_completions",
                           f"Removed {len(all_orphans)} orphaned routine completion records",
                           auto_fixed=True)
    except Exception as e:
        logger.warning("Error healer orphan routine completions cleanup failed: %s", e)


def _heal_stuck_pairing_codes():
    """Clean pairing codes that are past their expiry."""
    if not should_run(AGENT, "stuck_pairing", 7200):
        return
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM kiosk_pairing_codes WHERE expires_at < datetime('now')"
            )
            conn.commit()
            if result.rowcount > 0:
                log_action(AGENT, "stuck_pairing_cleanup",
                           f"Cleaned {result.rowcount} expired pairing codes",
                           auto_fixed=True)
    except Exception as e:
        logger.warning("Error healer pairing cleanup failed: %s", e)


def _check_log_file_growth():
    """Alert if log file is growing too fast (>20MB between rotations)."""
    if not should_run(AGENT, "log_growth", 3600):
        return
    try:
        if LOG_FILE.exists():
            size_mb = LOG_FILE.stat().st_size / (1024 * 1024)
            if size_mb > 20:
                log_action(AGENT, "log_growth_warning",
                           f"huddle.log is {size_mb:.1f}MB — approaching rotation threshold",
                           severity="warning")
    except Exception as e:
        logger.warning("Error healer log growth check failed: %s", e)


