"""Agent actions audit trail for self-healing agents."""
import sqlite3


def up(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            action_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            auto_fixed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            household_id INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_created "
        "ON agent_actions(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_agent "
        "ON agent_actions(agent, action_type)"
    )
