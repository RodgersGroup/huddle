"""Add medication stock tracking fields to selfcare_items."""

import logging

logger = logging.getLogger("huddle")


def up(conn):
    # Check which columns already exist
    cols = {row[1] for row in conn.execute("PRAGMA table_info(selfcare_items)").fetchall()}
    new_cols = {
        "box_quantity": "INTEGER",
        "box_total": "INTEGER",
        "repeats_remaining": "INTEGER",
        "repeats_total": "INTEGER",
        "dose_quantity": "INTEGER NOT NULL DEFAULT 1",
    }
    for col, typedef in new_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE selfcare_items ADD COLUMN {col} {typedef}")
            logger.info("Added column selfcare_items.%s", col)
    conn.commit()
