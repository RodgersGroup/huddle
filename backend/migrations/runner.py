"""Migration runner for Huddle.

ROLLBACK STRATEGY
-----------------
This migration system is forward-only: migrations have an up() function but
no down(). Rollback is manual and should follow this approach:

  1. Stop the service:  sudo systemctl stop chores-kiosk
  2. Restore the database from the most recent backup:
       gunzip -k backups/huddle_backup_YYYYMMDD_HHMMSS.db.gz
       cp backups/huddle_backup_YYYYMMDD_HHMMSS.db chores.db
  3. If the failed migration added columns or tables that need to be
     removed and no backup is recent enough, use the sqlite3 CLI to
     manually DROP TABLE or recreate the table without the new columns.
  4. Remove the migration's row from the _migrations table so it will
     be retried on next startup:
       DELETE FROM _migrations WHERE name = '<migration_name>';
  5. Restart the service:  sudo systemctl start chores-kiosk

Backups run every 6 hours via cron (scripts/backup.sh). Always verify
the backup timestamp before restoring.
"""
import logging
import sqlite3
import importlib
import pkgutil
from pathlib import Path

logger = logging.getLogger("huddle")

# Migrations that were renamed — old name → new name.
# If the old name is already recorded as applied, the new name is
# marked applied automatically so the (idempotent) migration doesn't re-run.
_RENAMED_MIGRATIONS = {
    "009_feedback": "033_feedback",
}


def run_migrations(conn: sqlite3.Connection):
    """Run all pending migrations in order."""
    # Create _migrations tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Handle renamed migrations: if old name is applied, record the new name too
    applied = {row[0] for row in conn.execute("SELECT name FROM _migrations").fetchall()}
    for old_name, new_name in _RENAMED_MIGRATIONS.items():
        if old_name in applied and new_name not in applied:
            conn.execute(
                "INSERT INTO _migrations (name, applied_at) VALUES (?, datetime('now'))",
                (new_name,)
            )
            conn.commit()
            applied.add(new_name)
            logger.info("Migration %s: aliased from renamed %s", new_name, old_name)

    # Find all migration modules
    migrations_dir = Path(__file__).parent
    migration_files = sorted(
        f.stem for f in migrations_dir.glob("[0-9]*.py")
    )

    for name in migration_files:
        if name in applied:
            logger.debug("Migration %s: already applied, skipping", name)
            continue

        logger.info("Running migration: %s", name)
        try:
            module = importlib.import_module(f"migrations.{name}")
            module.up(conn)
            conn.execute(
                "INSERT INTO _migrations (name, applied_at) VALUES (?, datetime('now'))",
                (name,)
            )
            conn.commit()
            logger.info("Migration %s: applied successfully", name)
        except Exception as e:
            conn.rollback()
            logger.error("Migration %s failed: %s", name, e, exc_info=True)
            raise
