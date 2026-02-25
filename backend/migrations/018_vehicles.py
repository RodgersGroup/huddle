"""Add vehicles and vehicle_maintenance tables for vehicle/maintenance tracking."""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            make TEXT,
            model TEXT,
            year INTEGER,
            rego_plate TEXT,
            rego_expiry TEXT,
            insurance_provider TEXT,
            insurance_expiry TEXT,
            odometer INTEGER,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL REFERENCES households(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_maintenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT,
            due_date TEXT,
            due_odometer INTEGER,
            completed INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            completed_by TEXT,
            cost REAL,
            recurrence TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL REFERENCES households(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_household ON vehicles(household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicle_maint_vehicle ON vehicle_maintenance(vehicle_id)")
