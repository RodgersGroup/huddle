"""Add override columns to meal_duties for one-off swap tracking."""


def up(conn):
    # Check if columns already exist (idempotent)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(meal_duties)").fetchall()]
    if "is_override" not in cols:
        conn.execute("ALTER TABLE meal_duties ADD COLUMN is_override INTEGER DEFAULT 0")
    if "override_original_index" not in cols:
        conn.execute("ALTER TABLE meal_duties ADD COLUMN override_original_index INTEGER")
    conn.commit()
