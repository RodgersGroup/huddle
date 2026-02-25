"""Add feedback_replies table for conversation threads on feedback items."""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
            reply_by TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER REFERENCES households(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_replies_feedback ON feedback_replies(feedback_id)")
