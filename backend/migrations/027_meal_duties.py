"""Add meal_duties table for cooking/baking/shopping rotation."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meal_duties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            duty_type TEXT NOT NULL,
            day_of_week INTEGER,
            current_person_index INTEGER DEFAULT 0,
            last_rotated_date TEXT,
            UNIQUE(household_id, duty_type, day_of_week),
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE INDEX IF NOT EXISTS idx_meal_duties_household
            ON meal_duties(household_id);
    """)
    conn.commit()
