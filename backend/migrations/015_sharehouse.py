"""Add sharehouse modules: expenses, house rules, tenancy."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        -- Shared expenses
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'AUD',
            paid_by TEXT NOT NULL,
            split_type TEXT DEFAULT 'equal',
            split_data TEXT,
            category TEXT DEFAULT 'other',
            receipt_note TEXT,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Per-person expense splits
        CREATE TABLE IF NOT EXISTS expense_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_id INTEGER NOT NULL,
            person TEXT NOT NULL,
            amount REAL NOT NULL,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES expenses(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Settlement records
        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paid_by TEXT NOT NULL,
            paid_to TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT,
            note TEXT,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Recurring shared costs
        CREATE TABLE IF NOT EXISTS shared_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            split_type TEXT DEFAULT 'equal',
            split_data TEXT,
            frequency TEXT DEFAULT 'monthly',
            due_day INTEGER,
            category TEXT DEFAULT 'rent',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- House rules
        CREATE TABLE IF NOT EXISTS house_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            category TEXT DEFAULT 'general',
            sort_order INTEGER DEFAULT 0,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- House rule acknowledgements
        CREATE TABLE IF NOT EXISTS house_rule_acknowledgements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            person TEXT NOT NULL,
            acknowledged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES house_rules(id) ON DELETE CASCADE,
            FOREIGN KEY (household_id) REFERENCES households(id),
            UNIQUE(rule_id, person, household_id)
        );

        -- Tenancy / lease details
        CREATE TABLE IF NOT EXISTS tenancy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT,
            landlord_name TEXT,
            landlord_contact TEXT,
            real_estate_agent TEXT,
            agent_contact TEXT,
            lease_start TEXT,
            lease_end TEXT,
            rent_amount REAL,
            rent_frequency TEXT DEFAULT 'weekly',
            bond_total REAL,
            bond_lodged_with TEXT DEFAULT 'fair_trading',
            bond_reference TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Per-person bond contributions
        CREATE TABLE IF NOT EXISTS bond_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            amount REAL NOT NULL,
            paid_date TEXT,
            notes TEXT,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Move-in/move-out checklists
        CREATE TABLE IF NOT EXISTS move_checklists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            checklist_type TEXT NOT NULL,
            items TEXT NOT NULL,
            status TEXT DEFAULT 'in_progress',
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_expenses_household ON expenses(household_id);
        CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(household_id, date);
        CREATE INDEX IF NOT EXISTS idx_expense_splits_person ON expense_splits(household_id, person);
        CREATE INDEX IF NOT EXISTS idx_settlements_household ON settlements(household_id);
        CREATE INDEX IF NOT EXISTS idx_shared_costs_household ON shared_costs(household_id);
        CREATE INDEX IF NOT EXISTS idx_house_rules_household ON house_rules(household_id);
        CREATE INDEX IF NOT EXISTS idx_tenancy_household ON tenancy(household_id);
        CREATE INDEX IF NOT EXISTS idx_bond_contributions_household ON bond_contributions(household_id);
        CREATE INDEX IF NOT EXISTS idx_move_checklists_household ON move_checklists(household_id);
    """)
    conn.commit()

    # Add household_type column
    try:
        conn.execute("ALTER TABLE households ADD COLUMN household_type TEXT DEFAULT 'family'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Seed new modules as disabled for existing households
    for module_key in ['expenses', 'house_rules', 'tenancy']:
        try:
            households = conn.execute("SELECT id FROM households").fetchall()
            for h in households:
                hid = h[0] if isinstance(h, tuple) else h["id"]
                existing = conn.execute(
                    "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = ?",
                    (hid, module_key)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, 0)",
                        (hid, module_key)
                    )
            conn.commit()
        except Exception as e:
            logger.warning("Migration 015: seed %s module: %s", module_key, e)
