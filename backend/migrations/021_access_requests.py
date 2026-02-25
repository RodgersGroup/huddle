"""Add access_requests table for manager-approval login flow."""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL REFERENCES households(id),
            member_id INTEGER NOT NULL REFERENCES household_members(id),
            token TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_token ON access_requests(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_household ON access_requests(household_id)")
