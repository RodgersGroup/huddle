"""Add segment column to households for segment-based pricing."""
import sqlite3
import logging

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    # Add segment column to households
    try:
        conn.execute(
            "ALTER TABLE households ADD COLUMN segment TEXT DEFAULT 'household'"
        )
        logger.info("Added segment column to households")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info("segment column already exists")
        else:
            raise

    # Set segment based on existing household_type
    segment_mappings = {
        'solo': 'solo',
        'couple': 'solo',
        'sharehouse': 'sharehouse',
        'student_house': 'sharehouse',
        'family': 'household',
        'other': 'household',
    }
    for htype, segment in segment_mappings.items():
        cursor = conn.execute(
            "UPDATE households SET segment = ? WHERE household_type = ?",
            (segment, htype)
        )
        if cursor.rowcount > 0:
            logger.info("Set segment '%s' for %d households with type '%s'",
                        segment, cursor.rowcount, htype)

    # Migrate old tier values to new system
    tier_mappings = {
        'plus': 'home',
        'premium': 'home_plus',
        'family': 'home_plus',
        'sharehouse': 'share',
    }
    for old_tier, new_tier in tier_mappings.items():
        cursor = conn.execute(
            "UPDATE households SET tier = ? WHERE tier = ?",
            (new_tier, old_tier)
        )
        if cursor.rowcount > 0:
            logger.info("Migrated %d households from tier '%s' to '%s'",
                        cursor.rowcount, old_tier, new_tier)

    conn.commit()
