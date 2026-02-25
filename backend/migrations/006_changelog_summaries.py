"""Cache table for LLM-generated changelog summaries.

Stores plain-English rewrites of git commit subjects so the status page
changelog is readable by non-technical household members.  Summaries are
generated once via local Ollama and cached permanently (commit hashes are
immutable).
"""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS changelog_summaries (
            hash TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    logger.info("Migration 006: created changelog_summaries table")
