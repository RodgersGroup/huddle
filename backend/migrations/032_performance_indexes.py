"""Add composite index on completions and index on chores(household_id) for query performance."""
import sqlite3


def up(conn: sqlite3.Connection):
    # Composite index for the common query pattern: fetch completions by household + chore, ordered by date
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_completions_household_completed "
        "ON completions(household_id, chore_id, completed_at DESC)"
    )
    # Index on chores(household_id) for the frequent WHERE household_id = ? filter
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chores_household_id "
        "ON chores(household_id)"
    )
