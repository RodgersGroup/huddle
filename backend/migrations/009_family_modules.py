"""Family modules: routines, rewards, allowances, pets."""


def up(conn):
    # ── Routines ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            routine_type TEXT NOT NULL DEFAULT 'morning',
            assigned_to TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS routine_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '✓',
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS routine_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            completed_by TEXT NOT NULL,
            completed_date TEXT NOT NULL,
            completed_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE,
            FOREIGN KEY (item_id) REFERENCES routine_items(id) ON DELETE CASCADE,
            UNIQUE(item_id, completed_date)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_routines_household ON routines(household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_routine_completions_date ON routine_completions(completed_date, routine_id)")

    # ── Rewards ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reward_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            points INTEGER NOT NULL,
            reason TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            awarded_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            cost INTEGER NOT NULL,
            icon TEXT DEFAULT '🎁',
            available INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reward_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reward_id INTEGER NOT NULL,
            person TEXT NOT NULL,
            points_spent INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            approved_by TEXT,
            approved_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (reward_id) REFERENCES rewards(id),
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reward_points_person ON reward_points(person, household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rewards_household ON rewards(household_id)")

    # ── Allowances ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS allowance_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 5.00,
            frequency TEXT NOT NULL DEFAULT 'weekly',
            pay_day INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            UNIQUE(person, household_id),
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS allowance_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            amount REAL NOT NULL,
            transaction_type TEXT NOT NULL,
            description TEXT,
            approved_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL,
            current_amount REAL DEFAULT 0,
            icon TEXT DEFAULT '🎯',
            completed INTEGER DEFAULT 0,
            completed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_allowance_tx_person ON allowance_transactions(person, household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_savings_goals_person ON savings_goals(person, household_id)")

    # ── Pets ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            species TEXT NOT NULL DEFAULT 'dog',
            breed TEXT,
            date_of_birth TEXT,
            icon TEXT DEFAULT '🐕',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pet_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pet_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'feeding',
            schedule_type TEXT NOT NULL DEFAULT 'daily',
            schedule_days TEXT,
            schedule_times TEXT,
            people TEXT,
            current_person_index INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (pet_id) REFERENCES pets(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pet_task_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            completed_by TEXT NOT NULL,
            scheduled_time TEXT,
            completed_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES pet_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pet_vet_appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pet_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT,
            vet_name TEXT,
            notes TEXT,
            completed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (pet_id) REFERENCES pets(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_household ON pets(household_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pet_tasks_pet ON pet_tasks(pet_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pet_completions_date ON pet_task_completions(completed_at, task_id)")

    # ── Seed new modules as disabled for existing households ──
    new_modules = ['routines', 'rewards', 'allowances', 'pets']
    for h in conn.execute("SELECT id FROM households").fetchall():
        for mod_key in new_modules:
            existing = conn.execute(
                "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = ?",
                (h[0], mod_key)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, 0)",
                    (h[0], mod_key)
                )
