"""Add freezer_items table for tracking freezer meals and leftovers."""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS freezer_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'meal',
            quantity INTEGER DEFAULT 1,
            date_frozen TEXT,
            expiry_date TEXT,
            notes TEXT,
            added_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER REFERENCES households(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_freezer_household ON freezer_items(household_id)")
