"""Add additional performance indexes for common query patterns."""
import sqlite3


def up(conn: sqlite3.Connection):
    indexes = [
        ("idx_completions_household", "completions", "household_id"),
        ("idx_completions_completed_at", "completions", "completed_at"),
        ("idx_calendar_events_household", "calendar_events", "household_id"),
        ("idx_calendar_events_start_date", "calendar_events", "start_date"),
        ("idx_adhoc_tasks_household", "adhoc_tasks", "household_id"),
        ("idx_bills_household", "bills", "household_id"),
        ("idx_bills_due_date", "bills", "due_date"),
    ]
    for idx_name, table, column in indexes:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({column})")
