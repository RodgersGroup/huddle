"""Preventive Scanner Agent — detects conditions that will cause errors before they happen."""
import logging
import shutil
from datetime import datetime
from pathlib import Path
from db import get_db, DB_PATH
from settings import get_people, get_all_settings
from agents.base import should_run, log_action, alert_managers

logger = logging.getLogger("huddle.agents")

AGENT = "preventive_scanner"
BACKUP_DIR = Path(__file__).parent.parent / "backups"


def run_system_checks():
    """Run system-wide preventive checks (not per-household)."""
    _check_disk_pressure()
    _check_memory_pressure()
    _check_database_integrity()
    _check_wal_file_size()
    _check_backup_freshness()


def run_household_checks(household_id: int):
    """Run per-household preventive checks."""
    _check_missing_settings(household_id)
    _check_foreign_key_health(household_id)
    _check_data_consistency(household_id)


def _check_disk_pressure():
    if not should_run(AGENT, "disk", 1800):
        return
    try:
        usage = shutil.disk_usage("/")
        percent = (usage.used / usage.total) * 100
        free_gb = usage.free / (1024 ** 3)
        if percent > 90:
            log_action(AGENT, "disk_critical",
                       f"Disk {percent:.0f}% full — only {free_gb:.1f}GB free",
                       severity="critical")
            alert_managers("Disk almost full", f"Only {free_gb:.1f}GB free ({percent:.0f}% used)")
        elif percent > 80:
            log_action(AGENT, "disk_warning",
                       f"Disk {percent:.0f}% full — {free_gb:.1f}GB free",
                       severity="warning")
    except Exception as e:
        logger.warning("Preventive scanner disk check failed: %s", e)


def _check_memory_pressure():
    if not should_run(AGENT, "memory", 1800):
        return
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        available_mb = meminfo.get("MemAvailable", 0) / 1024
        if available_mb < 100:
            log_action(AGENT, "memory_critical",
                       f"Only {available_mb:.0f}MB memory available",
                       severity="critical")
            alert_managers("Low memory", f"Only {available_mb:.0f}MB available — service may become unstable")
        elif available_mb < 200:
            log_action(AGENT, "memory_warning",
                       f"Memory getting low: {available_mb:.0f}MB available",
                       severity="warning")
    except Exception as e:
        logger.warning("Preventive scanner memory check failed: %s", e)


def _check_database_integrity():
    if not should_run(AGENT, "db_integrity", 43200):
        return
    try:
        with get_db() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] == "ok":
                log_action(AGENT, "db_integrity_ok", "Database integrity check passed")
            else:
                msg = result[0] if result else "unknown error"
                log_action(AGENT, "db_integrity_fail",
                           f"Database integrity check FAILED: {msg}",
                           severity="critical")
                alert_managers("Database integrity issue", f"PRAGMA quick_check returned: {msg}")
    except Exception as e:
        logger.warning("Preventive scanner DB integrity check failed: %s", e)


def _check_wal_file_size():
    if not should_run(AGENT, "wal_size", 3600):
        return
    try:
        wal_path = Path(str(DB_PATH) + "-wal")
        if wal_path.exists():
            wal_mb = wal_path.stat().st_size / (1024 * 1024)
            if wal_mb > 100:
                log_action(AGENT, "wal_critical",
                           f"WAL file is {wal_mb:.1f}MB — database may have issues",
                           severity="critical")
                alert_managers("Database WAL file too large",
                               f"WAL is {wal_mb:.1f}MB. This may indicate stuck transactions.")
            elif wal_mb > 50:
                log_action(AGENT, "wal_large",
                           f"WAL file is {wal_mb:.1f}MB — checkpoint may be stuck",
                           severity="warning")
                try:
                    with get_db() as conn:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    log_action(AGENT, "wal_checkpoint",
                               "Forced WAL checkpoint to reduce file size",
                               auto_fixed=True)
                except Exception as ce:
                    logger.warning("WAL checkpoint failed: %s", ce)
    except Exception as e:
        logger.warning("Preventive scanner WAL check failed: %s", e)


def _check_backup_freshness():
    if not should_run(AGENT, "backup_fresh", 21600):
        return
    try:
        if not BACKUP_DIR.exists():
            log_action(AGENT, "backup_missing",
                       "Backup directory does not exist",
                       severity="warning")
            return
        backups = sorted(BACKUP_DIR.glob("huddle_backup_*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            log_action(AGENT, "backup_none",
                       "No backups found — data is at risk",
                       severity="critical")
            alert_managers("No backups found", "The backup directory has no .db.gz files. Backups may have failed.")
            return
        newest = backups[0]
        age_hours = (datetime.now().timestamp() - newest.stat().st_mtime) / 3600
        if age_hours > 24:
            log_action(AGENT, "backup_critical",
                       f"Most recent backup is {age_hours:.0f} hours old — backups may be failing",
                       severity="critical")
            alert_managers("Backups are stale",
                           f"Last backup is {age_hours:.0f}h old. Check cron and backup.sh.")
        elif age_hours > 12:
            log_action(AGENT, "backup_stale",
                       f"Most recent backup is {age_hours:.0f} hours old ({newest.name})",
                       severity="warning")
    except Exception as e:
        logger.warning("Preventive scanner backup check failed: %s", e)


def _check_missing_settings(household_id: int):
    if not should_run(AGENT, f"settings_{household_id}", 43200):
        return
    try:
        people = get_people(household_id=household_id)
        if not people:
            log_action(AGENT, "no_members",
                       f"Household {household_id} has no members configured",
                       severity="warning", household_id=household_id)
        settings = get_all_settings(household_id=household_id)
        tz = settings.get("timezone", "")
        if not tz:
            log_action(AGENT, "no_timezone",
                       f"Household {household_id} has no timezone set",
                       severity="warning", household_id=household_id)
        import json
        members_raw = settings.get("household_members", "[]")
        try:
            json.loads(members_raw)
        except (json.JSONDecodeError, TypeError):
            log_action(AGENT, "broken_members_json",
                       f"Household {household_id} has corrupt household_members setting: {members_raw[:100]}",
                       severity="warning", household_id=household_id)
    except Exception as e:
        logger.warning("Preventive scanner settings check failed for household %d: %s", household_id, e)


def _check_foreign_key_health(household_id: int):
    if not should_run(AGENT, f"fk_health_{household_id}", 43200):
        return
    import sqlite3
    checks = [
        ("completions -> chores",
         "SELECT COUNT(*) FROM completions c LEFT JOIN chores ch ON c.chore_id = ch.id WHERE ch.id IS NULL AND c.household_id = ?"),
        ("bill_splits -> bills",
         "SELECT COUNT(*) FROM bill_splits bs LEFT JOIN bills b ON bs.bill_id = b.id WHERE b.id IS NULL AND bs.household_id = ?"),
        ("recipe_ingredients -> recipes",
         "SELECT COUNT(*) FROM recipe_ingredients ri LEFT JOIN recipes r ON ri.recipe_id = r.id WHERE r.id IS NULL AND r.household_id = ?"),
        ("routine_items -> routines",
         "SELECT COUNT(*) FROM routine_items ri LEFT JOIN routines r ON ri.routine_id = r.id WHERE r.id IS NULL AND r.household_id = ?"),
        ("poll_votes -> polls",
         "SELECT COUNT(*) FROM poll_votes pv LEFT JOIN polls p ON pv.poll_id = p.id WHERE p.id IS NULL AND p.household_id = ?"),
        ("selfcare_logs -> selfcare_items",
         "SELECT COUNT(*) FROM selfcare_logs sl LEFT JOIN selfcare_items si ON sl.item_id = si.id WHERE si.id IS NULL AND sl.household_id = ?"),
        ("pet_logs -> pets",
         "SELECT COUNT(*) FROM pet_logs pl LEFT JOIN pets p ON pl.pet_id = p.id WHERE p.id IS NULL AND pl.household_id = ?"),
    ]
    try:
        with get_db() as conn:
            issues = []
            for label, query in checks:
                try:
                    count = conn.execute(query, (household_id,)).fetchone()[0]
                    if count > 0:
                        issues.append(f"{label}: {count} orphans")
                except sqlite3.OperationalError:
                    pass
            if issues:
                log_action(AGENT, "fk_violations",
                           f"Found orphaned records in household {household_id}",
                           detail="; ".join(issues),
                           severity="warning", household_id=household_id)
    except Exception as e:
        logger.warning("Preventive scanner FK check failed for household %d: %s", household_id, e)


def _check_data_consistency(household_id: int):
    if not should_run(AGENT, f"consistency_{household_id}", 43200):
        return
    try:
        with get_db() as conn:
            bad_chores = conn.execute("""
                SELECT id, name, current_person_index, people FROM chores
                WHERE household_id = ? AND people IS NOT NULL AND people != '[]'
            """, (household_id,)).fetchall()
            import json
            for chore in bad_chores:
                try:
                    people_list = json.loads(chore["people"])
                    idx = chore["current_person_index"] or 0
                    if idx >= len(people_list) and people_list:
                        log_action(AGENT, "bad_chore_index",
                                   f'Chore "{chore["name"]}" has person index {idx} but only {len(people_list)} people',
                                   severity="warning", household_id=household_id)
                except (json.JSONDecodeError, TypeError):
                    log_action(AGENT, "bad_chore_people_json",
                               f'Chore "{chore["name"]}" has corrupt people JSON',
                               severity="warning", household_id=household_id)
            recurring = conn.execute("""
                SELECT id, title, recurrence_rule FROM calendar_events
                WHERE household_id = ? AND event_type = 'recurring' AND recurrence_rule IS NOT NULL
            """, (household_id,)).fetchall()
            for event in recurring:
                rule = event["recurrence_rule"]
                if rule and not any(rule.startswith(p) for p in ["FREQ=", "RRULE:", "{"]):
                    log_action(AGENT, "bad_recurrence_rule",
                               f'Calendar event "{event["title"]}" has unparseable recurrence: {rule[:80]}',
                               severity="warning", household_id=household_id)
    except Exception as e:
        logger.warning("Preventive scanner consistency check failed for household %d: %s", household_id, e)
