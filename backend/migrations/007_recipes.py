"""Recipe management system — recipes, ingredients, steps, tags tables."""

import logging
import sqlite3

logger = logging.getLogger("huddle")


def up(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            source_url TEXT,
            servings INTEGER DEFAULT 4,
            prep_time_mins INTEGER,
            cook_time_mins INTEGER,
            image_url TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            household_id INTEGER NOT NULL,
            FOREIGN KEY (household_id) REFERENCES households(id)
        );

        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            quantity TEXT,
            unit TEXT,
            section TEXT DEFAULT 'main',
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS recipe_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            instruction TEXT NOT NULL,
            section TEXT DEFAULT 'main',
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS recipe_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_recipes_household ON recipes(household_id);
        CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_recipe_steps_recipe ON recipe_steps(recipe_id);
        CREATE INDEX IF NOT EXISTS idx_recipe_tags_recipe ON recipe_tags(recipe_id);
    """)
    conn.commit()

    # Add recipe_id to meals table
    try:
        conn.execute("ALTER TABLE meals ADD COLUMN recipe_id INTEGER REFERENCES recipes(id)")
        conn.commit()
        logger.info("Migration 007: added recipe_id column to meals table")
    except Exception:
        pass  # Column may already exist

    # Seed the recipes module for all existing households
    try:
        households = conn.execute("SELECT id FROM households").fetchall()
        for h in households:
            hid = h[0] if isinstance(h, tuple) else h["id"]
            existing = conn.execute(
                "SELECT id FROM household_modules WHERE household_id = ? AND module_key = 'recipes'",
                (hid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO household_modules (household_id, module_key, enabled) VALUES (?, 'recipes', 1)",
                    (hid,)
                )
        conn.commit()
        logger.info("Migration 007: seeded recipes module for all households")
    except Exception as e:
        logger.warning("Migration 007: seed recipes module: %s", e)

    logger.info("Migration 007: created recipe tables (recipes, recipe_ingredients, recipe_steps, recipe_tags)")
