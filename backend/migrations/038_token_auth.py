"""Add password auth and session tokens (AnyList-style auth).

Adds:
- password_hash column to users table for bcrypt-hashed passwords
- sessions table for refresh token tracking and multi-device management
"""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return column in [row[1] for row in cursor.fetchall()]


def up(conn: sqlite3.Connection):
    # Add password_hash to users table
    if not _has_column(conn, "users", "password_hash"):
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT NULL")
        logger.info("038_token_auth: added password_hash to users")

    # Create sessions table for refresh token tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            household_id INTEGER NOT NULL REFERENCES households(id),
            refresh_token TEXT UNIQUE NOT NULL,
            device_id TEXT,
            device_name TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            revoked INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_refresh ON sessions(refresh_token) WHERE revoked = 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id) WHERE revoked = 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)"
    )
    conn.commit()
    logger.info("038_token_auth: created sessions table")
