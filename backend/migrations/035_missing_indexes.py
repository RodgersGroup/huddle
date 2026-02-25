"""Add missing indexes for tables that lack household_id indexes and compound indexes for dashboard hot-path queries.

Analysis summary:
- 18 tables had household_id columns but no index covering that column
- 5 dashboard summary queries were doing full table scans (inventory_items, reward_redemptions,
  pet_vet_appointments, vehicle_maintenance, allowance_settings)
- Several child tables lacked indexes on their foreign key columns (routine_items, expense_splits, feedback_replies)
- The meals table had no household_id index, causing full scans for the meal planner page
- The settings table (queried on every page load for timezone etc.) had no compound index on (household_id, key)
- The polls table had no household_id index, only a status index
"""
import sqlite3


def up(conn: sqlite3.Connection):
    indexes = [
        # =====================================================================
        # PRIORITY 1: Dashboard hot-path queries (modules.py summaries endpoint)
        # These fire on EVERY page load for every household member.
        # =====================================================================

        # meals: dashboard queries WHERE household_id=? AND day_of_week=? AND meal_type=?
        # Also covers: SELECT * FROM meals WHERE household_id=? ORDER BY day_of_week, meal_type
        ("idx_meals_household_dow",
         "CREATE INDEX IF NOT EXISTS idx_meals_household_dow ON meals(household_id, day_of_week, meal_type)"),

        # settings: queried on nearly every request for timezone, rotation order, etc.
        # WHERE key=? AND household_id=? -- compound covers both lookup patterns
        ("idx_settings_household_key",
         "CREATE INDEX IF NOT EXISTS idx_settings_household_key ON settings(household_id, key)"),

        # inventory_items: SCAN detected -- WHERE household_id=? AND quantity <= low_threshold
        ("idx_inventory_household",
         "CREATE INDEX IF NOT EXISTS idx_inventory_household ON inventory_items(household_id)"),

        # polls: SCAN when filtering by household -- WHERE household_id=? AND status='active'
        ("idx_polls_household_status",
         "CREATE INDEX IF NOT EXISTS idx_polls_household_status ON polls(household_id, status)"),

        # reward_redemptions: SCAN detected -- WHERE household_id=? AND status='pending'
        ("idx_redemptions_household_status",
         "CREATE INDEX IF NOT EXISTS idx_redemptions_household_status ON reward_redemptions(household_id, status)"),

        # pet_vet_appointments: SCAN detected -- WHERE household_id=? AND date>=? AND completed=0
        ("idx_vet_appts_household",
         "CREATE INDEX IF NOT EXISTS idx_vet_appts_household ON pet_vet_appointments(household_id, completed, date)"),

        # vehicle_maintenance: SCAN detected -- WHERE household_id=? AND completed=0
        ("idx_vehicle_maint_household",
         "CREATE INDEX IF NOT EXISTS idx_vehicle_maint_household ON vehicle_maintenance(household_id, completed)"),

        # allowance_settings: SCAN detected -- WHERE household_id=? AND active=1
        ("idx_allowance_settings_household",
         "CREATE INDEX IF NOT EXISTS idx_allowance_settings_household ON allowance_settings(household_id, active)"),

        # =====================================================================
        # PRIORITY 2: Foreign key indexes on child tables (prevent scans on JOINs/lookups)
        # =====================================================================

        # routine_items: SCAN detected -- WHERE routine_id=? (queried per-routine in dashboard loop)
        ("idx_routine_items_routine",
         "CREATE INDEX IF NOT EXISTS idx_routine_items_routine ON routine_items(routine_id)"),

        # expense_splits: SCAN detected -- WHERE expense_id=? (queried per-expense in list view)
        ("idx_expense_splits_expense",
         "CREATE INDEX IF NOT EXISTS idx_expense_splits_expense ON expense_splits(expense_id)"),

        # feedback_votes: covering index exists via UNIQUE but doesn't lead with household_id
        # WHERE household_id=? AND voter=? is a dashboard query
        ("idx_feedback_votes_household_voter",
         "CREATE INDEX IF NOT EXISTS idx_feedback_votes_household_voter ON feedback_votes(household_id, voter)"),

        # =====================================================================
        # PRIORITY 3: Remaining tables missing household_id indexes
        # Lower priority because they are less frequently queried or have small row counts,
        # but still important for multi-tenant isolation as the app scales.
        # =====================================================================

        # calendar_event_exceptions: queried via event_id (already indexed), but needs household_id
        ("idx_cal_exceptions_household",
         "CREATE INDEX IF NOT EXISTS idx_cal_exceptions_household ON calendar_event_exceptions(household_id)"),

        # push_subscriptions: SCAN detected -- WHERE household_id=?
        ("idx_push_subs_household",
         "CREATE INDEX IF NOT EXISTS idx_push_subs_household ON push_subscriptions(household_id)"),

        # poll_options: queried via poll_id, but needs household_id for tenant isolation
        ("idx_poll_options_household",
         "CREATE INDEX IF NOT EXISTS idx_poll_options_household ON poll_options(household_id)"),

        # poll_votes: queried via poll_id (already indexed), but needs household_id
        ("idx_poll_votes_household",
         "CREATE INDEX IF NOT EXISTS idx_poll_votes_household ON poll_votes(household_id)"),

        # household_devices: queried WHERE household_id=?
        ("idx_household_devices_household",
         "CREATE INDEX IF NOT EXISTS idx_household_devices_household ON household_devices(household_id)"),

        # kiosk_pairing_codes: queried WHERE household_id=?
        ("idx_pairing_codes_household",
         "CREATE INDEX IF NOT EXISTS idx_pairing_codes_household ON kiosk_pairing_codes(household_id)"),

        # routine_completions: has date index but not household_id
        ("idx_routine_completions_household",
         "CREATE INDEX IF NOT EXISTS idx_routine_completions_household ON routine_completions(household_id)"),

        # pet_tasks: has pet_id index but needs household_id for tenant queries
        ("idx_pet_tasks_household",
         "CREATE INDEX IF NOT EXISTS idx_pet_tasks_household ON pet_tasks(household_id)"),

        # pet_task_completions: needs household_id index
        ("idx_pet_completions_household",
         "CREATE INDEX IF NOT EXISTS idx_pet_completions_household ON pet_task_completions(household_id)"),

        # feedback_replies: SCAN detected -- WHERE feedback_id=? (already has idx but needs household too)
        ("idx_feedback_replies_household",
         "CREATE INDEX IF NOT EXISTS idx_feedback_replies_household ON feedback_replies(household_id)"),

        # house_rule_acknowledgements: needs household_id for tenant queries
        ("idx_rule_acks_household",
         "CREATE INDEX IF NOT EXISTS idx_rule_acks_household ON house_rule_acknowledgements(household_id)"),
    ]

    for name, sql in indexes:
        conn.execute(sql)

    # Update statistics so the query planner uses the new indexes
    conn.execute("ANALYZE")
