"""Add service interval fields to vehicle_maintenance for recurring services."""


def up(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(vehicle_maintenance)").fetchall()}
    new_cols = {
        "recurrence_km": "INTEGER",
        "recurrence_months": "INTEGER",
        "service_type": "TEXT",
        "odometer_at_completion": "INTEGER",
    }
    for col, typedef in new_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE vehicle_maintenance ADD COLUMN {col} {typedef}")
    conn.commit()
