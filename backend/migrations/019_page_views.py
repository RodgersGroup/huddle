"""Add page_views table for simple analytics."""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page TEXT NOT NULL,
            view_date TEXT NOT NULL DEFAULT (date('now')),
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(page, view_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_date ON page_views(view_date)")
