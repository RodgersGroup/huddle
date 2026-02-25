"""Initial schema — all tables, indexes, and legacy migrations.

This migration is idempotent: it uses CREATE TABLE IF NOT EXISTS and
wraps ALTER TABLE statements in try/except so it works on both fresh
databases and databases that already have some or all of these objects.
"""
import json
import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    # ------------------------------------------------------------------
    # 1. Core tables
    # ------------------------------------------------------------------
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            schedule_type TEXT NOT NULL,  -- 'daily', 'days', 'interval'
            schedule_days TEXT,           -- JSON array for specific days e.g. ["monday","thursday"]
            schedule_interval INTEGER,    -- interval in days
            people TEXT NOT NULL,         -- JSON array of assigned people
            current_person_index INTEGER DEFAULT 0,
            start_date TEXT,              -- optional start date for scheduling
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chore_id INTEGER NOT NULL,
            completed_by TEXT NOT NULL,
            completed_with TEXT,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chore_id) REFERENCES chores(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER NOT NULL,  -- 0=Monday, 6=Sunday
            meal_type TEXT NOT NULL,       -- 'lunch', 'dinner'
            meal_name TEXT NOT NULL,
            meal_variant TEXT,             -- 'all', 'ciara', 'keiran_tahni'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS adhoc_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            added_by TEXT,                -- person who added the task
            task_type TEXT DEFAULT 'request',  -- 'personal', 'request'
            assigned_to TEXT,             -- target person for 'request' type (null = anyone)
            completed INTEGER DEFAULT 0,
            completed_by TEXT,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            event_type TEXT NOT NULL DEFAULT 'one_off',  -- 'one_off', 'multi_day', 'recurring'
            start_date TEXT NOT NULL,     -- YYYY-MM-DD format
            end_date TEXT,                -- YYYY-MM-DD for multi-day events
            start_time TEXT,              -- HH:MM for timed events (null = all-day)
            end_time TEXT,                -- HH:MM for timed events
            all_day INTEGER DEFAULT 1,    -- 1 = all-day, 0 = timed
            participants TEXT NOT NULL,   -- JSON array: ["Keiran", "Ciara", "Tahni", "Guests"]
            recurrence_rule TEXT,         -- JSON: {"frequency": "weekly", "interval": 1, "end_date": "2025-12-31", "count": null}
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Table for storing exceptions (deleted instances) of recurring events
        CREATE TABLE IF NOT EXISTS calendar_event_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            exception_date TEXT NOT NULL,  -- YYYY-MM-DD of the instance to skip
            exception_type TEXT DEFAULT 'deleted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES calendar_events(id) ON DELETE CASCADE,
            UNIQUE(event_id, exception_date)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            subscription TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(person, subscription)
        );

        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL,
            due_date TEXT NOT NULL,
            recurrence TEXT DEFAULT 'once',
            paid INTEGER DEFAULT 0,
            paid_by TEXT,
            paid_at TIMESTAMP,
            category TEXT DEFAULT 'other',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'other',
            quantity INTEGER DEFAULT 1,
            unit TEXT,
            low_threshold INTEGER DEFAULT 1,
            added_by TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            created_by TEXT,
            poll_type TEXT DEFAULT 'single',
            status TEXT DEFAULT 'active',
            closes_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS poll_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            option_text TEXT NOT NULL,
            FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS poll_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            voter TEXT NOT NULL,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE,
            UNIQUE(poll_id, voter)
        );

        CREATE TABLE IF NOT EXISTS household_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            device_name TEXT NOT NULL,
            ip_address TEXT,
            last_seen TIMESTAMP,
            is_home INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Multi-tenant tables
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS households (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE,
            invite_code TEXT UNIQUE,
            invite_expires_at TIMESTAMP,
            tier TEXT DEFAULT 'free',
            timezone TEXT DEFAULT 'Australia/Sydney',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS household_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            color TEXT DEFAULT '#4ecdc4',
            role TEXT DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(household_id, user_id),
            UNIQUE(household_id, display_name)
        );

        CREATE TABLE IF NOT EXISTS magic_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            used INTEGER DEFAULT 0,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS kiosk_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
        );

        -- Module management: which modules are enabled per household
        CREATE TABLE IF NOT EXISTS household_modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            module_key TEXT NOT NULL,        -- e.g. 'chores', 'meals', 'calendar', 'bills', etc.
            enabled INTEGER DEFAULT 1,       -- 1=enabled, 0=disabled
            FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE,
            UNIQUE(household_id, module_key)
        );

        -- Indices for existing tables
        CREATE INDEX IF NOT EXISTS idx_completions_chore_id ON completions(chore_id);
        CREATE INDEX IF NOT EXISTS idx_completions_completed_at ON completions(completed_at);
        CREATE INDEX IF NOT EXISTS idx_meals_day ON meals(day_of_week);
        CREATE INDEX IF NOT EXISTS idx_adhoc_tasks_completed ON adhoc_tasks(completed);
        CREATE INDEX IF NOT EXISTS idx_calendar_events_start_date ON calendar_events(start_date);
        CREATE INDEX IF NOT EXISTS idx_calendar_events_end_date ON calendar_events(end_date);
        CREATE INDEX IF NOT EXISTS idx_calendar_event_exceptions_event_id ON calendar_event_exceptions(event_id);
        CREATE INDEX IF NOT EXISTS idx_calendar_event_exceptions_date ON calendar_event_exceptions(exception_date);
        CREATE INDEX IF NOT EXISTS idx_bills_due_date ON bills(due_date);
        CREATE INDEX IF NOT EXISTS idx_bills_paid ON bills(paid);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_category ON inventory_items(category);
        CREATE INDEX IF NOT EXISTS idx_polls_status ON polls(status);
        CREATE INDEX IF NOT EXISTS idx_poll_options_poll_id ON poll_options(poll_id);
        CREATE INDEX IF NOT EXISTS idx_poll_votes_poll_id ON poll_votes(poll_id);
        CREATE INDEX IF NOT EXISTS idx_household_devices_person ON household_devices(person);

        -- Indices for multi-tenant tables
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_households_slug ON households(slug);
        CREATE INDEX IF NOT EXISTS idx_households_invite_code ON households(invite_code);
        CREATE INDEX IF NOT EXISTS idx_household_members_household_id ON household_members(household_id);
        CREATE INDEX IF NOT EXISTS idx_household_members_user_id ON household_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_magic_links_token ON magic_links(token);
        CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email);
        CREATE INDEX IF NOT EXISTS idx_kiosk_tokens_token ON kiosk_tokens(token);
        CREATE INDEX IF NOT EXISTS idx_kiosk_tokens_household_id ON kiosk_tokens(household_id);
        CREATE INDEX IF NOT EXISTS idx_household_modules_household_id ON household_modules(household_id);
    """)
    conn.commit()

    # ------------------------------------------------------------------
    # 2. Legacy column migrations (idempotent via PRAGMA table_info)
    # ------------------------------------------------------------------

    # Migration: rename assigned_to to added_by if needed
    try:
        cursor = conn.execute("PRAGMA table_info(adhoc_tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'assigned_to' in columns and 'added_by' not in columns:
            conn.execute("ALTER TABLE adhoc_tasks RENAME COLUMN assigned_to TO added_by")
            conn.commit()
            logger.info("Migrated adhoc_tasks: assigned_to -> added_by")
    except Exception as e:
        logger.warning("Migration check: %s", e)

    # Migration: add task_type and assigned_to columns to adhoc_tasks
    try:
        cursor = conn.execute("PRAGMA table_info(adhoc_tasks)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'task_type' not in columns:
            conn.execute("ALTER TABLE adhoc_tasks ADD COLUMN task_type TEXT DEFAULT 'request'")
            conn.commit()
            logger.info("Migrated adhoc_tasks: added task_type column")
        if 'assigned_to' not in columns:
            conn.execute("ALTER TABLE adhoc_tasks ADD COLUMN assigned_to TEXT")
            conn.commit()
            logger.info("Migrated adhoc_tasks: added assigned_to column")
    except Exception as e:
        logger.warning("Migration check (task_type): %s", e)

    # Migration: add completed_with column to completions
    try:
        cursor = conn.execute("PRAGMA table_info(completions)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'completed_with' not in columns:
            conn.execute("ALTER TABLE completions ADD COLUMN completed_with TEXT")
            conn.commit()
            logger.info("Migrated completions: added completed_with column")
    except Exception as e:
        logger.warning("Migration check (completed_with): %s", e)

    # Migration: add household_id to all existing tables for multi-tenancy
    _tables_needing_household_id = [
        "chores",
        "completions",
        "meals",
        "adhoc_tasks",
        "calendar_events",
        "calendar_event_exceptions",
        "settings",
        "push_subscriptions",
        "bills",
        "inventory_items",
        "polls",
        "poll_options",
        "poll_votes",
        "household_devices",
    ]
    for table in _tables_needing_household_id:
        try:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            if 'household_id' not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN household_id INTEGER REFERENCES households(id)")
                conn.commit()
                logger.info("Migrated %s: added household_id column", table)
        except Exception as e:
            logger.warning("Migration check (household_id on %s): %s", table, e)

    # ------------------------------------------------------------------
    # 3. Seed household #1 for existing data
    # ------------------------------------------------------------------
    try:
        existing = conn.execute("SELECT id FROM households WHERE id = 1").fetchone()
        if not existing:
            # Read people from settings
            row = conn.execute("SELECT value FROM settings WHERE key = 'household_members'").fetchone()
            people = []
            if row:
                try:
                    people = json.loads(row[0] if isinstance(row, tuple) else row["value"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if not people:
                people = ["Keiran", "Ciara", "Tahni"]

            # Read member colors from settings
            color_row = conn.execute("SELECT value FROM settings WHERE key = 'member_colors'").fetchone()
            member_colors = {}
            if color_row:
                try:
                    member_colors = json.loads(color_row[0] if isinstance(color_row, tuple) else color_row["value"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if not member_colors:
                member_colors = {"Keiran": "#4ecdc4", "Ciara": "#ff6b9d", "Tahni": "#4caf50"}

            # Create the household
            conn.execute(
                "INSERT INTO households (id, name, slug, tier, timezone, created_at) "
                "VALUES (1, 'My Household', 'my-household', 'free', 'Australia/Sydney', CURRENT_TIMESTAMP)"
            )
            conn.commit()

            # Create placeholder users and household_members
            for i, person in enumerate(people):
                email = f"{person.lower()}@localhost"
                conn.execute(
                    "INSERT OR IGNORE INTO users (email, display_name, created_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (email, person)
                )
                conn.commit()
                user_row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                user_id = user_row[0] if isinstance(user_row, tuple) else user_row["id"]
                color = member_colors.get(person, "#4ecdc4")
                role = "manager" if i == 0 else "member"
                conn.execute(
                    "INSERT OR IGNORE INTO household_members (household_id, user_id, display_name, color, role, joined_at) "
                    "VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (user_id, person, color, role)
                )
            conn.commit()

            # Set household_id=1 on all existing rows in all tables
            for table in _tables_needing_household_id:
                try:
                    conn.execute(f"UPDATE {table} SET household_id = 1 WHERE household_id IS NULL")
                except Exception as e:
                    logger.warning("Migration backfill (%s): %s", table, e)
            conn.commit()

            logger.info("Migrated: created household #1 and backfilled household_id on all existing data")
    except Exception as e:
        logger.warning("Migration check (create household #1): %s", e)

    # ------------------------------------------------------------------
    # 4. Seed default modules for all households
    # ------------------------------------------------------------------
    _default_modules = [
        ("chores", 1),
        ("adhoc_tasks", 1),
        ("meals", 1),
        ("calendar", 1),
        ("bills", 1),
        ("inventory", 1),
        ("polls", 1),
        ("fuel", 1),
        ("weather", 1),
    ]
    try:
        households_rows = conn.execute("SELECT id FROM households").fetchall()
        for h in households_rows:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing_modules = conn.execute(
                "SELECT module_key FROM household_modules WHERE household_id = ?", (hid,)
            ).fetchall()
            existing_keys = {(r[0] if isinstance(r, tuple) else r["module_key"]) for r in existing_modules}
            for module_key, enabled in _default_modules:
                if module_key not in existing_keys:
                    conn.execute(
                        "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, ?)",
                        (hid, module_key, enabled)
                    )
            conn.commit()
    except Exception as e:
        logger.warning("Migration check (seed modules): %s", e)
