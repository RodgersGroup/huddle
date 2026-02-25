"""Add kiosk_pairing_codes table for QR-based kiosk pairing."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kiosk_pairing_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            household_id INTEGER,
            kiosk_token_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            claimed_at TEXT,
            claim_ip TEXT,
            request_ip TEXT,
            FOREIGN KEY (household_id) REFERENCES households(id),
            FOREIGN KEY (kiosk_token_id) REFERENCES kiosk_tokens(id)
        );

        CREATE INDEX IF NOT EXISTS idx_kiosk_pairing_codes_code ON kiosk_pairing_codes(code);
        CREATE INDEX IF NOT EXISTS idx_kiosk_pairing_codes_expires ON kiosk_pairing_codes(expires_at);
    """)
    conn.commit()
