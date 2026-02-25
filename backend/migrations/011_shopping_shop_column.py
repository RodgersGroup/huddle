"""Add shop column to shopping_items for store selection."""
import sqlite3


def up(conn: sqlite3.Connection):
    # Add shop column (nullable, stores the shop name)
    try:
        conn.execute("ALTER TABLE shopping_items ADD COLUMN shop TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
