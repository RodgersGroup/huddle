"""Add PIN-based authentication for dependents (no email required)."""
import sqlite3


def up(conn: sqlite3.Connection):
    # Add PIN hash column to users table
    try:
        conn.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add PIN lockout tracking
    try:
        conn.execute("ALTER TABLE users ADD COLUMN pin_failed_attempts INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE users ADD COLUMN pin_locked_until TEXT")
    except sqlite3.OperationalError:
        pass
