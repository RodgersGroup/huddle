"""Seed vehicles as a separate module for existing households."""


def up(conn):
    for h in conn.execute("SELECT id FROM households").fetchall():
        existing = conn.execute(
            "SELECT 1 FROM household_modules WHERE household_id = ? AND module_key = ?",
            (h["id"], "vehicles"),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, ?, 1)",
                (h["id"], "vehicles"),
            )
