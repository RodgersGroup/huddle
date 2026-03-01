"""Recipe management — CRUD, URL import, shopping list & meal plan integration."""

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from urllib.request import Request as URLRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import TenantContext, get_current_user, require_role
from db import get_db, check_version
from websocket import manager

logger = logging.getLogger("huddle")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper: build a full recipe dict from DB rows
# ---------------------------------------------------------------------------

def _recipe_dict(conn, recipe_row):
    """Combine a recipes row with its ingredients, steps, and tags."""
    r = dict(recipe_row)
    rid = r["id"]
    r["ingredients"] = [dict(row) for row in conn.execute(
        "SELECT * FROM recipe_ingredients WHERE recipe_id = ? ORDER BY sort_order", (rid,)
    ).fetchall()]
    r["steps"] = [dict(row) for row in conn.execute(
        "SELECT * FROM recipe_steps WHERE recipe_id = ? ORDER BY step_number", (rid,)
    ).fetchall()]
    r["tags"] = [row["tag"] for row in conn.execute(
        "SELECT tag FROM recipe_tags WHERE recipe_id = ?", (rid,)
    ).fetchall()]
    return r


def _insert_children(conn, recipe_id, ingredients, steps, tags):
    """Insert ingredient, step, and tag rows for a recipe."""
    for i, ing in enumerate(ingredients):
        if not isinstance(ing, dict):
            continue
        name = (ing.get("name") or "").strip()
        if not name:
            continue
        quantity = ing.get("quantity", "")
        unit = ing.get("unit", "")
        # If no quantity/unit provided, try to parse them from the name text
        if not quantity and not unit:
            parsed = _parse_ingredient_text(name)
            name = parsed["name"]
            quantity = parsed.get("quantity", "")
            unit = parsed.get("unit", "")
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, name, quantity, unit, section, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
            (recipe_id, name, quantity, unit, ing.get("section", "main"), i),
        )
    for i, step in enumerate(steps, 1):
        if isinstance(step, dict):
            text = (step.get("instruction") or "").strip()
            section = step.get("section", "main")
        elif isinstance(step, str):
            text = step.strip()
            section = "main"
        else:
            continue
        if not text:
            continue
        conn.execute(
            "INSERT INTO recipe_steps (recipe_id, step_number, instruction, section) VALUES (?, ?, ?, ?)",
            (recipe_id, i, text, section),
        )
    for tag in tags:
        tag_str = (str(tag)).strip().lower()
        if tag_str:
            conn.execute(
                "INSERT INTO recipe_tags (recipe_id, tag) VALUES (?, ?)",
                (recipe_id, tag_str),
            )


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.get("/api/recipes")
def get_recipes(tenant: TenantContext = Depends(get_current_user), since: str | None = Query(None)):
    """List all recipes with tags and ingredient count. Pass ?since=<timestamp> for delta sync."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            if since:
                rows = conn.execute("""
                    SELECT r.*,
                           GROUP_CONCAT(DISTINCT t.tag) AS tags,
                           COUNT(DISTINCT ri.id) AS ingredient_count
                    FROM recipes r
                    LEFT JOIN recipe_tags t ON t.recipe_id = r.id
                    LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                    WHERE r.household_id = ? AND r.deleted_at IS NULL AND r.updated_at > ?
                    GROUP BY r.id
                    ORDER BY r.name
                """, (household_id, since)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT r.*,
                           GROUP_CONCAT(DISTINCT t.tag) AS tags,
                           COUNT(DISTINCT ri.id) AS ingredient_count
                    FROM recipes r
                    LEFT JOIN recipe_tags t ON t.recipe_id = r.id
                    LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                    WHERE r.household_id = ? AND r.deleted_at IS NULL
                    GROUP BY r.id
                    ORDER BY r.name
                """, (household_id,)).fetchall()
            recipes = []
            for row in rows:
                d = dict(row)
                d["tags"] = d["tags"].split(",") if d["tags"] else []
                recipes.append(d)
            response = {"recipes": recipes}
            if since:
                deleted_rows = conn.execute(
                    "SELECT id FROM recipes WHERE household_id = ? AND deleted_at > ?",
                    (household_id, since),
                ).fetchall()
                response["deleted"] = [r["id"] for r in deleted_rows]
            return response
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/recipes/search")
def search_recipes(q: str = "", tenant: TenantContext = Depends(get_current_user)):
    """Search recipes by name or tag."""
    household_id = tenant.household_id
    try:
        query = q.strip()
        if not query:
            return {"recipes": []}
        like = f"%{query}%"
        with get_db() as conn:
            rows = conn.execute("""
                SELECT DISTINCT r.*,
                       GROUP_CONCAT(DISTINCT t.tag) AS tags,
                       COUNT(DISTINCT ri.id) AS ingredient_count
                FROM recipes r
                LEFT JOIN recipe_tags t ON t.recipe_id = r.id
                LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                WHERE r.household_id = ? AND r.deleted_at IS NULL
                  AND (r.name LIKE ? OR t.tag LIKE ?)
                GROUP BY r.id
                ORDER BY r.name
            """, (household_id, like, like)).fetchall()
            recipes = []
            for row in rows:
                d = dict(row)
                d["tags"] = d["tags"].split(",") if d["tags"] else []
                recipes.append(d)
            return {"recipes": recipes}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in search_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in search_recipes: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Get a single recipe with ingredients, steps, and tags."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (recipe_id, household_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Recipe not found")
            return _recipe_dict(conn, row)
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/recipes")
async def create_recipe(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Create a recipe manually."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get("name", "").strip() if isinstance(data.get("name"), str) else ""
        if not name:
            logger.warning("Validation failed in create_recipe: %s", "Recipe name is required")
            raise HTTPException(status_code=400, detail="Recipe name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_recipe: %s", "Recipe name is too long")
            raise HTTPException(status_code=400, detail="Recipe name is too long (max 200 characters)")

        ingredients = data.get("ingredients", [])
        if not isinstance(ingredients, list) or not ingredients:
            logger.warning("Validation failed in create_recipe: %s", "At least one ingredient is required")
            raise HTTPException(status_code=400, detail="At least one ingredient is required")

        steps = data.get("steps", [])
        if not isinstance(steps, list):
            steps = []

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        # --- End validation ---

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO recipes (name, description, source_url, servings, prep_time_mins, cook_time_mins,
                                     image_url, notes, created_by, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                (data.get("description") or "").strip(),
                (data.get("source_url") or "").strip() or None,
                data.get("servings", 4),
                data.get("prep_time_mins"),
                data.get("cook_time_mins"),
                data.get("image_url") or None,
                (data.get("notes") or "").strip() or None,
                tenant.display_name,
                household_id,
            ))
            recipe_id = cursor.lastrowid
            _insert_children(conn, recipe_id, ingredients, steps, tags)
            conn.commit()

        await manager.broadcast({"type": "recipes_updated"}, household_id=household_id)
        return {"id": recipe_id, "name": name, "message": "Recipe created"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/recipes/{recipe_id}")
async def update_recipe(recipe_id: int, request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Update a recipe (full replacement of children)."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM recipes WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (recipe_id, household_id),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Recipe not found")
            check_version(data, existing, "recipe")

            # --- Input validation ---
            name = data.get("name", existing["name"])
            if isinstance(name, str):
                name = name.strip()
            if not name:
                logger.warning("Validation failed in update_recipe: %s", "Recipe name is required")
                raise HTTPException(status_code=400, detail="Recipe name is required")
            if len(name) > 200:
                logger.warning("Validation failed in update_recipe: %s", "Recipe name is too long")
                raise HTTPException(status_code=400, detail="Recipe name is too long (max 200 characters)")
            # --- End validation ---

            conn.execute("""
                UPDATE recipes SET name = ?, description = ?, source_url = ?, servings = ?,
                    prep_time_mins = ?, cook_time_mins = ?, image_url = ?, notes = ?, updated_at = ?,
                    version = COALESCE(version, 0) + 1
                WHERE id = ? AND household_id = ?
            """, (
                name,
                data.get("description", existing["description"]),
                data.get("source_url", existing["source_url"]),
                data.get("servings", existing["servings"]),
                data.get("prep_time_mins", existing["prep_time_mins"]),
                data.get("cook_time_mins", existing["cook_time_mins"]),
                data.get("image_url", existing["image_url"]),
                data.get("notes", existing["notes"]),
                datetime.now().isoformat(),
                recipe_id,
                household_id,
            ))

            # Replace children if provided
            if "ingredients" in data:
                conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
                _insert_children(conn, recipe_id, data["ingredients"], [], [])
            if "steps" in data:
                conn.execute("DELETE FROM recipe_steps WHERE recipe_id = ?", (recipe_id,))
                _insert_children(conn, recipe_id, [], data["steps"], [])
            if "tags" in data:
                conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
                _insert_children(conn, recipe_id, [], [], data["tags"])

            conn.commit()

        await manager.broadcast({"type": "recipes_updated"}, household_id=household_id)
        return {"message": "Recipe updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/recipes/{recipe_id}")
async def delete_recipe(recipe_id: int, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Delete a recipe (cascades to ingredients, steps, tags)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "UPDATE recipes SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND household_id = ?",
                (recipe_id, household_id),
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Recipe not found")
            # Unlink from any meals
            conn.execute(
                "UPDATE meals SET recipe_id = NULL WHERE recipe_id = ? AND household_id = ?",
                (recipe_id, household_id),
            )
            conn.commit()

        await manager.broadcast({"type": "recipes_updated"}, household_id=household_id)
        return {"message": "Recipe deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_recipe: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Import from URL (JSON-LD schema.org/Recipe)
# ---------------------------------------------------------------------------

@router.post("/api/recipes/import-url")
async def import_recipe_from_url(request: Request, tenant: TenantContext = Depends(require_role("manager", "member"))):
    """Fetch a URL and extract recipe data from JSON-LD structured data."""
    household_id = tenant.household_id
    try:
        data = await request.json()
        url = (data.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="URL is required")

        try:
            recipe_data = _extract_recipe_from_url(url)
        except Exception as exc:
            logger.error("Recipe import failed for %s: %s", url, exc)
            raise HTTPException(status_code=422, detail="Could not extract recipe from that URL. Try adding it manually.")

        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO recipes (name, description, source_url, servings, prep_time_mins, cook_time_mins,
                                     image_url, notes, created_by, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recipe_data["name"],
                recipe_data.get("description", ""),
                url,
                recipe_data.get("servings", 4),
                recipe_data.get("prep_time"),
                recipe_data.get("cook_time"),
                recipe_data.get("image"),
                "",
                tenant.display_name,
                household_id,
            ))
            recipe_id = cursor.lastrowid
            _insert_children(
                conn,
                recipe_id,
                recipe_data.get("ingredients", []),
                recipe_data.get("steps", []),
                [],
            )
            conn.commit()

        await manager.broadcast({"type": "recipes_updated"}, household_id=household_id)
        return {
            "id": recipe_id,
            "name": recipe_data["name"],
            "ingredient_count": len(recipe_data.get("ingredients", [])),
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in import_recipe_from_url: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in import_recipe_from_url: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# AI-powered tag suggestion
# ---------------------------------------------------------------------------


@router.post("/api/recipes/{recipe_id}/suggest-tags")
async def suggest_tags(recipe_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Use Ollama to suggest tags for a recipe based on its name, description, and ingredients."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            recipe = conn.execute(
                "SELECT * FROM recipes WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
                (recipe_id, household_id)
            ).fetchone()
            if not recipe:
                raise HTTPException(status_code=404, detail="Recipe not found")

            ingredient_rows = conn.execute(
                "SELECT name FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe_id,)
            ).fetchall()
            ingredient_names = [r["name"] for r in ingredient_rows]

        from ollama_helper import suggest_recipe_tags
        tags = await suggest_recipe_tags(
            name=recipe["name"],
            description=recipe["description"] or "",
            ingredients=ingredient_names,
        )
        return {"tags": tags}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error suggesting tags: %s", e, exc_info=True)
        return {"tags": []}


# ---------------------------------------------------------------------------
# Add to shopping list / meal plan
# ---------------------------------------------------------------------------

@router.post("/api/recipes/{recipe_id}/to-shopping")
async def recipe_to_shopping(recipe_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add recipe ingredients to the shopping list with smart dedup via Ollama."""
    household_id = tenant.household_id
    try:
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()

        ingredient_ids = body.get("ingredient_ids")  # None = all, list = specific
        check_pantry = body.get("check_pantry", True)
        scale = body.get("scale", 1.0)
        try:
            scale = float(scale)
            if scale <= 0 or scale > 100:
                scale = 1.0
        except (TypeError, ValueError):
            scale = 1.0

        with get_db() as conn:
            recipe = conn.execute(
                "SELECT id, name FROM recipes WHERE id = ? AND household_id = ?",
                (recipe_id, household_id),
            ).fetchone()
            if not recipe:
                raise HTTPException(status_code=404, detail="Recipe not found")

            if ingredient_ids and isinstance(ingredient_ids, list):
                placeholders = ",".join("?" * len(ingredient_ids))
                ingredients = conn.execute(
                    f"SELECT * FROM recipe_ingredients WHERE recipe_id = ? AND id IN ({placeholders})",
                    [recipe_id] + ingredient_ids,
                ).fetchall()
            else:
                ingredients = conn.execute(
                    "SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
                ).fetchall()

            # Filter out pantry items first
            candidates = []
            skipped = 0
            for ing in ingredients:
                if check_pantry:
                    in_pantry = conn.execute(
                        "SELECT 1 FROM inventory_items WHERE household_id = ? AND LOWER(name) = LOWER(?) AND quantity > 0",
                        (household_id, ing["name"]),
                    ).fetchone()
                    if in_pantry:
                        skipped += 1
                        continue
                candidates.append(dict(ing))

            # Get existing unpurchased shopping items for dedup
            existing_items = conn.execute(
                "SELECT id, name, quantity FROM shopping_items WHERE household_id = ? AND purchased = 0",
                (household_id,),
            ).fetchall()
            existing_list = [dict(r) for r in existing_items]

            # Try Ollama smart dedup if there are both candidates and existing items
            matches = {}
            cleaned_names = {}
            if candidates and existing_list:
                try:
                    from helpers.ollama import find_duplicates
                    new_items = [{"name": c["name"]} for c in candidates]
                    result = await asyncio.to_thread(find_duplicates, new_items, existing_list)
                    matches = result.get("matches", {})
                    cleaned_names = result.get("cleaned", {})
                    logger.info("Ollama dedup: %d matches, %d cleaned names", len(matches), len(cleaned_names))
                except Exception as e:
                    logger.warning("Ollama dedup failed, falling back to exact match: %s", e)

            added = 0
            merged = 0
            new_item_ids = []
            for ing in candidates:
                qty = ""
                raw_qty = ing["quantity"] or ""
                if raw_qty and scale != 1.0:
                    raw_qty = _scale_quantity(raw_qty, scale)
                if raw_qty and ing["unit"]:
                    qty = f"{raw_qty} {ing['unit']}"
                elif raw_qty:
                    qty = raw_qty

                ing_name = ing["name"]

                # Check Ollama fuzzy match first
                if ing_name in matches:
                    existing_id = matches[ing_name]
                    existing_row = conn.execute(
                        "SELECT id, quantity, notes FROM shopping_items WHERE id = ? AND household_id = ?",
                        (existing_id, household_id),
                    ).fetchone()
                    if existing_row:
                        # Merge: append quantity info
                        old_qty = existing_row["quantity"] or ""
                        new_qty = f"{old_qty}, +{qty}" if old_qty and qty else old_qty or qty
                        old_notes = existing_row["notes"] or ""
                        new_notes = f"{old_notes}; For: {recipe['name']}" if old_notes else f"For: {recipe['name']}"
                        conn.execute(
                            "UPDATE shopping_items SET quantity = ?, notes = ? WHERE id = ? AND household_id = ?",
                            (new_qty, new_notes, existing_id, household_id),
                        )
                        merged += 1
                        continue

                # Fallback: exact case-insensitive match
                on_list = conn.execute(
                    "SELECT id, quantity FROM shopping_items WHERE household_id = ? AND LOWER(name) = LOWER(?) AND purchased = 0",
                    (household_id, ing_name),
                ).fetchone()
                if on_list:
                    # Merge quantity
                    old_qty = on_list["quantity"] or ""
                    new_qty = f"{old_qty}, +{qty}" if old_qty and qty else old_qty or qty
                    conn.execute(
                        "UPDATE shopping_items SET quantity = ? WHERE id = ? AND household_id = ?",
                        (new_qty, on_list["id"], household_id),
                    )
                    merged += 1
                    continue

                # Use cleaned name if Ollama fixed a typo
                final_name = cleaned_names.get(ing_name, ing_name)

                cursor = conn.execute(
                    "INSERT INTO shopping_items (name, quantity, category, notes, added_by, household_id) VALUES (?, ?, 'groceries', ?, ?, ?)",
                    (final_name, qty, f"For: {recipe['name']}", tenant.display_name, household_id),
                )
                new_item_ids.append(cursor.lastrowid)
                added += 1

            conn.commit()

        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)

        # Auto-categorize new items in background
        if new_item_ids:
            from routers.shopping import _auto_categorize_items
            asyncio.create_task(_auto_categorize_items(new_item_ids, household_id))

        msg = f"Added {added} item{'s' if added != 1 else ''} to shopping list"
        if merged:
            msg += f", merged {merged} duplicate{'s' if merged != 1 else ''}"
        if skipped:
            msg += f" ({skipped} already in pantry)"
        return {"added": added, "merged": merged, "skipped": skipped, "message": msg}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in recipe_to_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in recipe_to_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/recipes/{recipe_id}/to-meal")
async def recipe_to_meal(recipe_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Assign a recipe to a meal plan slot."""
    household_id = tenant.household_id
    try:
        body = await request.json()
        day_of_week = body.get("day_of_week")
        meal_type = body.get("meal_type", "dinner")
        meal_variant = body.get("meal_variant", "all")

        if day_of_week is None or day_of_week not in range(7):
            raise HTTPException(status_code=400, detail="day_of_week must be 0-6")
        if meal_type not in ("lunch", "dinner"):
            raise HTTPException(status_code=400, detail="meal_type must be lunch or dinner")

        with get_db() as conn:
            recipe = conn.execute(
                "SELECT id, name FROM recipes WHERE id = ? AND household_id = ?",
                (recipe_id, household_id),
            ).fetchone()
            if not recipe:
                raise HTTPException(status_code=404, detail="Recipe not found")

            existing = conn.execute(
                "SELECT id FROM meals WHERE household_id = ? AND day_of_week = ? AND meal_type = ? AND meal_variant = ?",
                (household_id, day_of_week, meal_type, meal_variant),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE meals SET meal_name = ?, recipe_id = ? WHERE id = ?",
                    (recipe["name"], recipe_id, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO meals (day_of_week, meal_type, meal_name, meal_variant, recipe_id, household_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (day_of_week, meal_type, recipe["name"], meal_variant, recipe_id, household_id),
                )
            conn.commit()

        await manager.broadcast({"type": "meals_updated"}, household_id=household_id)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return {"message": f"Added '{recipe['name']}' to {days[day_of_week]} {meal_type}"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in recipe_to_meal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in recipe_to_meal: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------

def _extract_recipe_from_url(url):
    """Fetch a URL and extract recipe data from JSON-LD schema.org markup."""
    req = URLRequest(url, headers={"User-Agent": "Huddle Recipe Importer/1.0"})
    with urlopen(req, timeout=15) as response:
        html = response.read().decode("utf-8", errors="replace")

    json_ld_pattern = re.compile(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )

    for match in json_ld_pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
            recipe = _find_recipe_in_jsonld(data)
            if recipe:
                return _parse_schema_recipe(recipe, url)
        except json.JSONDecodeError:
            continue

    raise ValueError("No recipe structured data found on page")


def _find_recipe_in_jsonld(data):
    """Recursively find a Recipe object in JSON-LD data."""
    if isinstance(data, list):
        for item in data:
            result = _find_recipe_in_jsonld(item)
            if result:
                return result
    elif isinstance(data, dict):
        schema_type = data.get("@type", "")
        if isinstance(schema_type, list):
            if "Recipe" in schema_type:
                return data
        elif schema_type == "Recipe":
            return data
        if "@graph" in data:
            return _find_recipe_in_jsonld(data["@graph"])
    return None


def _parse_iso_duration(duration):
    """Parse ISO 8601 duration (PT30M, PT1H30M) to minutes."""
    if not duration:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(duration))
    if m:
        hours = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        return hours * 60 + mins
    return None


def _parse_schema_recipe(data, source_url):
    """Convert a schema.org Recipe object into our internal format."""
    name = data.get("name", "Imported Recipe")
    description = data.get("description", "")

    # Servings
    servings = 4
    yield_val = data.get("recipeYield")
    if yield_val:
        if isinstance(yield_val, list):
            yield_val = yield_val[0]
        nums = re.findall(r"\d+", str(yield_val))
        if nums:
            servings = int(nums[0])

    prep_time = _parse_iso_duration(data.get("prepTime"))
    cook_time = _parse_iso_duration(data.get("cookTime"))

    # Image
    image = None
    img = data.get("image")
    if isinstance(img, str):
        image = img
    elif isinstance(img, list) and img:
        image = img[0] if isinstance(img[0], str) else img[0].get("url")
    elif isinstance(img, dict):
        image = img.get("url")

    # Ingredients
    raw_ingredients = data.get("recipeIngredient", [])
    ingredients = []
    for ing_text in raw_ingredients:
        if isinstance(ing_text, str):
            ingredients.append(_parse_ingredient_text(ing_text.strip()))

    # Steps
    steps = []
    instructions = data.get("recipeInstructions", [])
    if isinstance(instructions, str):
        steps = [s.strip() for s in instructions.split("\n") if s.strip()]
    elif isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, str):
                steps.append(item.strip())
            elif isinstance(item, dict):
                if item.get("@type") == "HowToStep":
                    steps.append(item.get("text", "").strip())
                elif item.get("@type") == "HowToSection":
                    for sub in item.get("itemListElement", []):
                        if isinstance(sub, dict):
                            steps.append(sub.get("text", "").strip())

    return {
        "name": name,
        "description": description,
        "servings": servings,
        "prep_time": prep_time,
        "cook_time": cook_time,
        "image": image,
        "ingredients": ingredients,
        "steps": [s for s in steps if s],
    }


def _scale_quantity(qty_str, scale):
    """Scale a quantity string by a factor. Handles fractions like '1 1/2'."""
    try:
        # Try to parse as a number (handles "2", "0.5", etc.)
        val = float(qty_str)
        scaled = val * scale
        # Format nicely: drop decimal if whole number
        if scaled == int(scaled):
            return str(int(scaled))
        return f"{scaled:.2f}".rstrip('0').rstrip('.')
    except ValueError:
        pass
    # Try mixed fraction like "1 1/2"
    m = re.match(r'^(\d+)\s+(\d+)/(\d+)$', qty_str.strip())
    if m:
        whole, num, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
        val = (whole + num / den) * scale
        if val == int(val):
            return str(int(val))
        return f"{val:.2f}".rstrip('0').rstrip('.')
    # Try simple fraction like "1/2"
    m = re.match(r'^(\d+)/(\d+)$', qty_str.strip())
    if m:
        val = (int(m.group(1)) / int(m.group(2))) * scale
        if val == int(val):
            return str(int(val))
        return f"{val:.2f}".rstrip('0').rstrip('.')
    # Can't parse — return as-is
    return qty_str


def _parse_ingredient_text(text):
    """Best-effort parse of ingredient text like '1 1/2 cups all-purpose flour'."""
    units = [
        r"cups?", r"tbsp", r"tablespoons?", r"tsp", r"teaspoons?", r"oz", r"ounces?",
        r"lbs?", r"pounds?", r"grams?", r"kg", r"kilograms?", r"ml", r"millilitres?",
        r"litres?", r"pinch(?:es)?", r"bunch(?:es)?", r"cloves?", r"cans?", r"packets?",
        r"pieces?", r"slices?", r"sprigs?", r"heads?", r"stalks?",
        r"g\b", r"l\b",  # Single-letter units require word boundary
    ]
    unit_pattern = "|".join(units)
    match = re.match(
        rf"^([\d\s/\u00bd\u00bc\u00be\u2153\u2154.-]+)?\s*({unit_pattern})?\s*(?:of\s+)?(.+)",
        text,
        re.IGNORECASE,
    )
    if match:
        quantity = (match.group(1) or "").strip()
        unit = (match.group(2) or "").strip()
        name = (match.group(3) or text).strip().rstrip(",").strip()
        return {"quantity": quantity, "unit": unit, "name": name}
    return {"quantity": "", "unit": "", "name": text}
