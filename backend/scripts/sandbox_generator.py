"""Sandbox household generator — creates demo households with rich preset data."""
import secrets
import string
import sqlite3
from datetime import datetime, timedelta

INVITE_CHARSET = string.ascii_uppercase.replace("O", "").replace("I", "").replace("L", "") + "23456789"


def _random_invite_code(length=8):
    return "".join(secrets.choice(INVITE_CHARSET) for _ in range(length))


# ── Preset definitions ──────────────────────────────────────────────

PRESETS = {
    "family": {
        "name": "The Demo Family",
        "members": [
            {"name": "Mum", "role": "manager", "color": "#ff6b9d"},
            {"name": "Dad", "role": "manager", "color": "#4caf50"},
            {"name": "Sophie", "role": "member", "color": "#9c27b0"},
            {"name": "Liam", "role": "dependent", "color": "#2196f3"},
        ],
        "chores": [
            {"name": "Unpack dishwasher", "schedule_type": "daily", "people": ["Sophie", "Liam"]},
            {"name": "Vacuum living room", "schedule_type": "days", "schedule_days": ["Monday", "Thursday"], "people": ["Mum", "Dad"]},
            {"name": "Take bins out", "schedule_type": "days", "schedule_days": ["Wednesday"], "people": ["Dad"]},
            {"name": "Clean bathrooms", "schedule_type": "days", "schedule_days": ["Saturday"], "people": ["Sophie", "Mum"]},
            {"name": "Mop kitchen floor", "schedule_type": "days", "schedule_days": ["Sunday"], "people": ["Dad", "Liam"]},
            {"name": "Feed the dog", "schedule_type": "daily", "people": ["Liam"]},
            {"name": "Water plants", "schedule_type": "interval", "schedule_interval": 3, "people": ["Sophie"]},
        ],
        "meals": [
            {"day": 0, "type": "dinner", "name": "Spaghetti Bolognese"},
            {"day": 1, "type": "dinner", "name": "Chicken Stir-fry"},
            {"day": 2, "type": "dinner", "name": "Fish & Chips"},
            {"day": 3, "type": "dinner", "name": "Tacos"},
            {"day": 4, "type": "dinner", "name": "Pizza Night"},
            {"day": 5, "type": "dinner", "name": "Roast Chicken"},
            {"day": 6, "type": "dinner", "name": "Leftovers / Takeaway"},
        ],
        "shopping": [
            {"name": "Milk", "category": "dairy", "quantity": "2L"},
            {"name": "Bread", "category": "bakery", "quantity": "1 loaf"},
            {"name": "Bananas", "category": "fruit_veg", "quantity": "1 bunch"},
            {"name": "Chicken breast", "category": "meat", "quantity": "500g"},
            {"name": "Pasta", "category": "pantry", "quantity": "500g"},
            {"name": "Tomato sauce", "category": "pantry"},
            {"name": "Laundry detergent", "category": "cleaning"},
            {"name": "Orange juice", "category": "drinks", "quantity": "1L"},
            {"name": "Dog food", "category": "other", "quantity": "1 bag"},
        ],
        "bills": [
            {"name": "Electricity", "amount": 285.00, "recurrence": "quarterly", "category": "utilities"},
            {"name": "Internet", "amount": 89.00, "recurrence": "monthly", "category": "utilities"},
            {"name": "Water", "amount": 180.00, "recurrence": "quarterly", "category": "utilities"},
            {"name": "Car Insurance", "amount": 125.00, "recurrence": "monthly", "category": "insurance"},
        ],
        "calendar": [
            {"title": "Sophie's ballet", "event_type": "recurring", "start_time": "16:00", "end_time": "17:00", "participants": ["Sophie", "Mum"],
             "recurrence_rule": {"frequency": "weekly", "interval": 1, "day_of_week": 2}},
            {"title": "Liam's soccer training", "event_type": "recurring", "start_time": "17:30", "end_time": "18:30", "participants": ["Liam", "Dad"],
             "recurrence_rule": {"frequency": "weekly", "interval": 1, "day_of_week": 3}},
            {"title": "Family movie night", "event_type": "recurring", "start_time": "19:00", "end_time": "21:00", "participants": ["Mum", "Dad", "Sophie", "Liam"],
             "recurrence_rule": {"frequency": "weekly", "interval": 1, "day_of_week": 5}},
        ],
        "polls": [
            {"question": "What should we do this weekend?", "options": ["Beach", "Movies", "Bushwalk", "Stay home"]},
        ],
    },
    "sharehouse": {
        "name": "42 Smith St Sharehouse",
        "members": [
            {"name": "Alex", "role": "manager", "color": "#4ecdc4"},
            {"name": "Jordan", "role": "member", "color": "#ff9800"},
            {"name": "Taylor", "role": "member", "color": "#e91e63"},
            {"name": "Sam", "role": "member", "color": "#8bc34a"},
        ],
        "chores": [
            {"name": "Clean kitchen", "schedule_type": "days", "schedule_days": ["Monday", "Thursday"], "people": ["Alex", "Jordan", "Taylor", "Sam"]},
            {"name": "Take bins out", "schedule_type": "days", "schedule_days": ["Tuesday"], "people": ["Alex", "Jordan", "Taylor", "Sam"]},
            {"name": "Vacuum common areas", "schedule_type": "days", "schedule_days": ["Saturday"], "people": ["Alex", "Jordan", "Taylor", "Sam"]},
            {"name": "Clean bathroom", "schedule_type": "days", "schedule_days": ["Sunday"], "people": ["Alex", "Jordan", "Taylor", "Sam"]},
            {"name": "Mop floors", "schedule_type": "days", "schedule_days": ["Wednesday"], "people": ["Alex", "Jordan", "Taylor", "Sam"]},
        ],
        "meals": [
            {"day": 0, "type": "dinner", "name": "Alex cooks"},
            {"day": 2, "type": "dinner", "name": "Jordan cooks"},
            {"day": 4, "type": "dinner", "name": "Taylor cooks"},
            {"day": 6, "type": "dinner", "name": "Sam cooks"},
        ],
        "shopping": [
            {"name": "Toilet paper", "category": "cleaning", "quantity": "12 pack"},
            {"name": "Dish soap", "category": "cleaning"},
            {"name": "Paper towels", "category": "cleaning", "quantity": "4 pack"},
            {"name": "Coffee", "category": "drinks", "quantity": "1kg"},
            {"name": "Oat milk", "category": "dairy", "quantity": "1L"},
        ],
        "bills": [
            {"name": "Rent", "amount": 2800.00, "recurrence": "monthly", "category": "housing"},
            {"name": "Electricity", "amount": 320.00, "recurrence": "quarterly", "category": "utilities"},
            {"name": "Internet", "amount": 79.00, "recurrence": "monthly", "category": "utilities"},
            {"name": "Contents Insurance", "amount": 45.00, "recurrence": "monthly", "category": "insurance"},
        ],
        "calendar": [
            {"title": "House meeting", "event_type": "recurring", "start_time": "19:00", "end_time": "19:30",
             "participants": ["Alex", "Jordan", "Taylor", "Sam"],
             "recurrence_rule": {"frequency": "monthly", "interval": 1, "day_of_week": 0, "week_of_month": 1}},
        ],
        "polls": [
            {"question": "Should we get a shared Netflix account?", "options": ["Yes", "No", "Already have one"]},
        ],
    },
    "couple": {
        "name": "Home Sweet Home",
        "members": [
            {"name": "Emma", "role": "manager", "color": "#ff6b9d"},
            {"name": "James", "role": "manager", "color": "#4ecdc4"},
        ],
        "chores": [
            {"name": "Cook dinner", "schedule_type": "daily", "people": ["Emma", "James"]},
            {"name": "Wash dishes", "schedule_type": "daily", "people": ["Emma", "James"]},
            {"name": "Vacuum", "schedule_type": "days", "schedule_days": ["Saturday"], "people": ["James"]},
            {"name": "Laundry", "schedule_type": "days", "schedule_days": ["Wednesday", "Sunday"], "people": ["Emma"]},
            {"name": "Grocery shopping", "schedule_type": "days", "schedule_days": ["Sunday"], "people": ["Emma", "James"]},
        ],
        "meals": [
            {"day": 0, "type": "dinner", "name": "Salmon & Rice"},
            {"day": 1, "type": "dinner", "name": "Pasta Carbonara"},
            {"day": 2, "type": "dinner", "name": "Thai Green Curry"},
            {"day": 3, "type": "dinner", "name": "Steak & Salad"},
            {"day": 4, "type": "dinner", "name": "Takeaway"},
            {"day": 5, "type": "dinner", "name": "Homemade Pizza"},
            {"day": 6, "type": "dinner", "name": "Roast Lamb"},
        ],
        "shopping": [
            {"name": "Avocados", "category": "fruit_veg", "quantity": "4"},
            {"name": "Salmon fillets", "category": "meat", "quantity": "2"},
            {"name": "Sourdough bread", "category": "bakery"},
            {"name": "White wine", "category": "drinks"},
            {"name": "Greek yoghurt", "category": "dairy"},
        ],
        "bills": [
            {"name": "Mortgage", "amount": 2400.00, "recurrence": "monthly", "category": "housing"},
            {"name": "Electricity", "amount": 180.00, "recurrence": "quarterly", "category": "utilities"},
            {"name": "Internet", "amount": 69.00, "recurrence": "monthly", "category": "utilities"},
            {"name": "Home Insurance", "amount": 165.00, "recurrence": "monthly", "category": "insurance"},
        ],
        "calendar": [
            {"title": "Date night", "event_type": "recurring", "start_time": "18:30", "end_time": "21:00",
             "participants": ["Emma", "James"],
             "recurrence_rule": {"frequency": "weekly", "interval": 1, "day_of_week": 4}},
        ],
        "polls": [
            {"question": "Where should we go for our anniversary?", "options": ["Byron Bay", "Melbourne", "Hunter Valley", "Stay home & cook"]},
        ],
    },
}


def create_sandbox(conn: sqlite3.Connection, preset_key: str, creator_user_id: int) -> dict:
    """Create a sandbox household with rich demo data.

    Returns dict with household info: {id, name, slug, invite_code, preset, member_count}
    """
    if preset_key not in PRESETS:
        raise ValueError(f"Unknown preset: {preset_key}. Valid: {', '.join(PRESETS.keys())}")

    preset = PRESETS[preset_key]
    now = datetime.now().isoformat()
    today = datetime.now().date()
    invite_code = _random_invite_code()
    slug = f"sandbox-{preset_key}-{secrets.token_hex(3)}"

    # Create household
    cursor = conn.execute(
        """INSERT INTO households (name, slug, invite_code, tier, timezone, created_at, created_by)
           VALUES (?, ?, ?, 'free', 'Australia/Sydney', ?, ?)""",
        (f"[Sandbox] {preset['name']}", slug, invite_code, now, creator_user_id),
    )
    household_id = cursor.lastrowid

    # Enable all modules
    modules = ["chores", "adhoc_tasks", "meals", "calendar", "bills", "inventory", "polls", "shopping", "recipes", "feedback"]
    for mod in modules:
        conn.execute(
            "INSERT OR IGNORE INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, 1)",
            (household_id, mod),
        )

    # Create members
    member_user_ids = {}
    member_names = []
    member_colors = {}
    for m in preset["members"]:
        email = f"sandbox_{secrets.token_hex(4)}@placeholder"
        cur = conn.execute(
            "INSERT INTO users (email, display_name, created_at) VALUES (?, ?, ?)",
            (email, m["name"], now),
        )
        uid = cur.lastrowid
        conn.execute(
            """INSERT INTO household_members (household_id, user_id, display_name, role, color, joined_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (household_id, uid, m["name"], m["role"], m["color"], now),
        )
        member_user_ids[m["name"]] = uid
        member_names.append(m["name"])
        member_colors[m["name"]] = m["color"]

    # Seed settings
    import json
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
        ("household_members", json.dumps(member_names), household_id),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, household_id) VALUES (?, ?, ?)",
        ("member_colors", json.dumps(member_colors), household_id),
    )

    # Create chores
    for chore in preset.get("chores", []):
        conn.execute(
            """INSERT INTO chores (name, schedule_type, schedule_days, schedule_interval,
               people, current_person_index, start_date, created_at, household_id)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                chore["name"],
                chore["schedule_type"],
                json.dumps(chore.get("schedule_days", [])),
                chore.get("schedule_interval", 1),
                json.dumps(chore["people"]),
                today.isoformat(),
                now,
                household_id,
            ),
        )

    # Create meals
    for meal in preset.get("meals", []):
        conn.execute(
            "INSERT INTO meals (day_of_week, meal_type, meal_name, meal_variant, created_at, household_id) VALUES (?, ?, ?, 'all', ?, ?)",
            (meal["day"], meal["type"], meal["name"], now, household_id),
        )

    # Create shopping items
    for item in preset.get("shopping", []):
        conn.execute(
            """INSERT INTO shopping_items (name, quantity, category, notes, added_by, purchased, created_at, household_id)
               VALUES (?, ?, ?, '', ?, 0, ?, ?)""",
            (item["name"], item.get("quantity", ""), item.get("category", "other"), member_names[0], now, household_id),
        )

    # Create bills
    for bill in preset.get("bills", []):
        due_date = (today + timedelta(days=secrets.randbelow(28) + 1)).isoformat()
        conn.execute(
            """INSERT INTO bills (name, amount, due_date, recurrence, paid, category, notes, created_at, household_id)
               VALUES (?, ?, ?, ?, 0, ?, '', ?, ?)""",
            (bill["name"], bill["amount"], due_date, bill["recurrence"], bill.get("category", "other"), now, household_id),
        )

    # Create calendar events
    for event in preset.get("calendar", []):
        start_date = today.isoformat()
        conn.execute(
            """INSERT INTO calendar_events (title, event_type, start_date, start_time, end_time,
               all_day, participants, recurrence_rule, created_at, household_id)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                event["title"],
                event["event_type"],
                start_date,
                event.get("start_time"),
                event.get("end_time"),
                json.dumps(event.get("participants", [])),
                json.dumps(event.get("recurrence_rule")) if event.get("recurrence_rule") else None,
                now,
                household_id,
            ),
        )

    # Create polls
    for poll in preset.get("polls", []):
        cur = conn.execute(
            "INSERT INTO polls (question, created_by, poll_type, status, created_at, household_id) VALUES (?, ?, 'single', 'active', ?, ?)",
            (poll["question"], member_names[0], now, household_id),
        )
        poll_id = cur.lastrowid
        for option in poll["options"]:
            conn.execute(
                "INSERT INTO poll_options (poll_id, option_text) VALUES (?, ?)",
                (poll_id, option),
            )

    conn.commit()

    return {
        "id": household_id,
        "name": f"[Sandbox] {preset['name']}",
        "slug": slug,
        "invite_code": invite_code,
        "preset": preset_key,
        "member_count": len(preset["members"]),
    }


def delete_sandbox(conn: sqlite3.Connection, household_id: int):
    """Delete a sandbox household and all its data."""
    # Verify it's a sandbox
    hh = conn.execute("SELECT name FROM households WHERE id = ?", (household_id,)).fetchone()
    if not hh:
        raise ValueError("Household not found")
    if not hh["name"].startswith("[Sandbox]"):
        raise ValueError("Not a sandbox household — refusing to delete")

    # Get member user_ids to clean up placeholder users
    members = conn.execute(
        "SELECT user_id FROM household_members WHERE household_id = ?",
        (household_id,),
    ).fetchall()
    member_ids = [m["user_id"] for m in members]

    # Delete all related data (cascading FKs handle most, but be explicit)
    tables_with_household = [
        "chores", "completions", "meals", "adhoc_tasks", "calendar_events",
        "bills", "inventory_items", "polls", "shopping_items", "recipes",
        "feedback", "feedback_votes", "settings", "household_modules",
        "household_members", "push_subscriptions",
    ]
    for table in tables_with_household:
        try:
            conn.execute(f"DELETE FROM {table} WHERE household_id = ?", (household_id,))
        except sqlite3.OperationalError:
            pass  # Table might not exist

    # Delete kiosk tokens
    try:
        conn.execute("DELETE FROM kiosk_tokens WHERE household_id = ?", (household_id,))
    except sqlite3.OperationalError:
        pass

    # Delete the household itself
    conn.execute("DELETE FROM households WHERE id = ?", (household_id,))

    # Clean up placeholder users (only if they don't belong to other households)
    for uid in member_ids:
        other = conn.execute(
            "SELECT 1 FROM household_members WHERE user_id = ? LIMIT 1",
            (uid,),
        ).fetchone()
        if not other:
            user = conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
            if user and "@placeholder" in user["email"]:
                conn.execute("DELETE FROM users WHERE id = ?", (uid,))

    conn.commit()
