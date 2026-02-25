"""Add EV charging support: battery_kwh/is_ev on vehicles, ev_charging_sessions table."""


def up(conn):
    # Add EV fields to vehicles
    try:
        conn.execute("ALTER TABLE vehicles ADD COLUMN is_ev INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE vehicles ADD COLUMN battery_kwh REAL DEFAULT NULL")
    except Exception:
        pass

    # Create charging sessions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ev_charging_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL,
            charge_date TEXT NOT NULL,
            start_percent INTEGER,
            end_percent INTEGER,
            kwh_added REAL,
            cost REAL,
            location TEXT,
            charger_type TEXT DEFAULT 'home',
            notes TEXT,
            logged_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ev_charging_vehicle
        ON ev_charging_sessions(vehicle_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ev_charging_household
        ON ev_charging_sessions(household_id)
    """)
