import asyncio
import json
import logging
import sqlite3
import urllib.request
import urllib.error
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import get_current_user, TenantContext
from datetime import datetime
from db import get_db
from websocket import manager
from push import send_push_to_person_bg
from settings import get_people

logger = logging.getLogger("huddle")

router = APIRouter()


_CATEGORY_KEYWORDS = {
    "fruit_veg": ["apple", "banana", "orange", "lemon", "lime", "avocado", "tomato", "potato", "onion",
        "garlic", "carrot", "broccoli", "spinach", "lettuce", "cucumber", "capsicum", "pepper",
        "mushroom", "zucchini", "corn", "peas", "beans", "celery", "ginger", "chilli", "herb",
        "basil", "parsley", "coriander", "mint", "salad", "kale", "cabbage", "pumpkin", "sweet potato",
        "beetroot", "eggplant", "grape", "strawberry", "blueberry", "raspberry", "mango", "pineapple",
        "watermelon", "pear", "peach", "plum", "kiwi", "cherry", "fruit", "veg", "vegetable",
        "spring onion", "shallot", "leek", "asparagus", "radish", "turnip", "broccolini"],
    "dairy": ["milk", "cheese", "yoghurt", "yogurt", "butter", "cream", "egg", "eggs", "sour cream",
        "cottage cheese", "ricotta", "mozzarella", "parmesan", "cheddar", "feta", "brie", "halloumi",
        "cream cheese", "custard", "margarine"],
    "meat": ["chicken", "beef", "pork", "lamb", "mince", "steak", "sausage", "bacon", "ham", "fish",
        "salmon", "tuna", "prawn", "shrimp", "crab", "seafood", "turkey", "veal", "duck", "rissole",
        "schnitzel", "chop", "fillet", "drumstick", "wing", "thigh", "breast", "deli", "salami",
        "chorizo", "pepperoni"],
    "bakery": ["bread", "roll", "bun", "wrap", "tortilla", "pita", "croissant", "muffin", "bagel",
        "sourdough", "rye", "brioche", "flatbread", "naan", "crumpet", "english muffin", "pastry",
        "pie", "scone", "loaf", "baguette"],
    "pantry": ["rice", "pasta", "noodle", "sauce", "oil", "vinegar", "flour", "sugar", "salt",
        "spice", "pepper", "cinnamon", "cumin", "paprika", "turmeric", "oregano", "thyme",
        "can", "canned", "tin", "tinned", "cereal", "oats", "muesli", "honey", "jam", "peanut butter",
        "nutella", "vegemite", "stock", "broth", "coconut", "lentil", "chickpea", "kidney bean",
        "baked beans", "soy sauce", "sriracha", "ketchup", "mustard", "mayo", "mayonnaise",
        "tomato paste", "passata", "diced tomato", "cracker", "biscuit", "chip", "nut", "almond",
        "cashew", "walnut", "seed", "dried"],
    "frozen": ["frozen", "ice cream", "ice block", "fish finger", "nugget", "pizza", "frozen veg",
        "frozen meal", "gelato", "sorbet", "frozen chip", "frozen pie"],
    "drinks": ["water", "juice", "soft drink", "coke", "pepsi", "sprite", "lemonade", "coffee",
        "tea", "beer", "wine", "spirit", "vodka", "rum", "whisky", "bourbon", "gin", "soda",
        "sparkling", "kombucha", "cordial", "energy drink", "milk alternative", "oat milk",
        "almond milk", "soy milk"],
    "cleaning": ["detergent", "bleach", "wipe", "sponge", "dishwash", "laundry", "fabric softener",
        "toilet", "disinfectant", "spray", "cleaner", "mop", "broom", "garbage bag", "bin liner",
        "paper towel", "tissue", "toilet paper", "soap", "hand wash", "shampoo", "conditioner",
        "toothpaste", "toothbrush", "deodorant", "razor", "sunscreen", "moisturiser", "body wash",
        "glad wrap", "foil", "alfoil", "cling wrap", "ziplock"],
}


def _rule_based_categorize(name: str) -> str:
    """Fast keyword-based categorization for shopping items."""
    lower = name.lower().strip()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return category
    return "other"


async def _auto_categorize_items(item_ids: list[int], household_id: int):
    """Background task: categorize items using keyword rules, fallback to Ollama."""
    try:
        with get_db() as conn:
            placeholders = ",".join("?" * len(item_ids))
            rows = conn.execute(
                f"SELECT id, name, category FROM shopping_items WHERE id IN ({placeholders}) AND household_id = ?",
                item_ids + [household_id],
            ).fetchall()

            to_categorize = [dict(r) for r in rows if r["category"] in ("other", "groceries", "")]
            if not to_categorize:
                return

            # First pass: rule-based (fast, reliable)
            updated = 0
            still_uncategorized = []
            for item in to_categorize:
                new_cat = _rule_based_categorize(item["name"])
                if new_cat != "other":
                    conn.execute(
                        "UPDATE shopping_items SET category = ? WHERE id = ? AND household_id = ?",
                        (new_cat, item["id"], household_id),
                    )
                    updated += 1
                else:
                    still_uncategorized.append(item)

            # Second pass: try Ollama for remaining items
            if still_uncategorized:
                try:
                    from helpers.ollama import categorize_items
                    names = [item["name"] for item in still_uncategorized]
                    categories = await asyncio.to_thread(categorize_items, names)
                    for item in still_uncategorized:
                        new_cat = categories.get(item["name"])
                        if new_cat and new_cat != "other":
                            conn.execute(
                                "UPDATE shopping_items SET category = ? WHERE id = ? AND household_id = ?",
                                (new_cat, item["id"], household_id),
                            )
                            updated += 1
                except Exception as e:
                    logger.debug("Ollama categorization unavailable: %s", e)

            if updated:
                conn.commit()
                await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
                logger.info("Auto-categorized %d shopping items for household %d", updated, household_id)

    except Exception as e:
        logger.warning("Auto-categorize background task failed: %s", e)


@router.get("/api/shopping")
def get_shopping(tenant: TenantContext = Depends(get_current_user)):
    """Get all shopping items, unpurchased first."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM shopping_items
                WHERE household_id = ?
                ORDER BY purchased ASC, created_at DESC
            """, (household_id,)).fetchall()
            return {"items": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in get_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in get_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/shopping")
async def create_shopping_item(request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Add an item to the shopping list."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        # --- Input validation ---
        name = data.get('name', '').strip() if isinstance(data.get('name'), str) else ''
        if not name:
            logger.warning("Validation failed in create_shopping_item: %s", "Item name is required")
            raise HTTPException(status_code=400, detail="Item name is required")
        if len(name) > 200:
            logger.warning("Validation failed in create_shopping_item: %s", "Item name is too long (max 200 characters)")
            raise HTTPException(status_code=400, detail="Item name is too long (max 200 characters)")

        quantity = data.get('quantity', '')
        if isinstance(quantity, str) and len(quantity) > 100:
            logger.warning("Validation failed in create_shopping_item: %s", "Quantity is too long (max 100 characters)")
            raise HTTPException(status_code=400, detail="Quantity is too long (max 100 characters)")

        notes = data.get('notes', '')
        if isinstance(notes, str) and len(notes) > 500:
            logger.warning("Validation failed in create_shopping_item: %s", "Notes too long (max 500 characters)")
            raise HTTPException(status_code=400, detail="Notes too long (max 500 characters)")
        # --- End validation ---

        category = data.get('category', 'other')
        auto_categorize = category in ('other', 'groceries', '')

        with get_db() as conn:
            # Free tier limit (only enforced post-beta)
            from tiers import FREE_TIER_LIMITS, BETA_MODE
            if not BETA_MODE:
                h_row = conn.execute("SELECT tier FROM households WHERE id = ?", (household_id,)).fetchone()
                h_tier = h_row["tier"] if h_row and h_row["tier"] else "free"
                if h_tier == "free":
                    limit = FREE_TIER_LIMITS.get("shopping")
                    if limit:
                        count = conn.execute("SELECT COUNT(*) FROM shopping_items WHERE household_id = ? AND purchased = 0", (household_id,)).fetchone()[0]
                        if count >= limit:
                            raise HTTPException(status_code=403, detail=f"Free plan is limited to {limit} shopping items. Upgrade to add more.")

            # Smart duplicate detection: check for existing unpurchased item with same name
            existing = conn.execute(
                "SELECT id, quantity FROM shopping_items WHERE LOWER(name) = LOWER(?) AND purchased = 0 AND household_id = ?",
                (name, household_id)
            ).fetchone()

            if existing:
                # Merge: bump quantity if numeric, otherwise just note the duplicate
                old_qty = existing['quantity'] or ''
                new_qty = data.get('quantity', '') or ''
                try:
                    merged_qty = str(int(float(old_qty or '1')) + int(float(new_qty or '1')))
                except (ValueError, TypeError):
                    merged_qty = f"{old_qty}, {new_qty}".strip(', ') if new_qty else old_qty
                conn.execute("UPDATE shopping_items SET quantity = ? WHERE id = ? AND household_id = ?", (merged_qty, existing['id'], household_id))
                conn.commit()
                item_id = existing['id']
                await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
                return {"id": item_id, "message": "Already on list — quantity updated", "merged": True}

            cursor = conn.execute("""
                INSERT INTO shopping_items (name, quantity, category, notes, added_by, shop, household_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                data.get('quantity', ''),
                category,
                data.get('notes', ''),
                data.get('added_by'),
                data.get('shop', ''),
                household_id,
            ))
            conn.commit()
            item_id = cursor.lastrowid

        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)

        # Notify others that an item was added to the shopping list
        added_by = data.get('added_by') or tenant.display_name
        try:
            for person in get_people(household_id=household_id):
                if person != added_by:
                    send_push_to_person_bg(
                        person,
                        "Huddle: Shopping list updated",
                        f"{added_by} added '{name}' to the shopping list",
                        "shopping-added",
                        household_id=household_id,
                        module="shopping",
                    )
        except Exception as push_err:
            logger.warning("Shopping push notification failed: %s", push_err)

        # Auto-categorize in background if no specific category was chosen
        if auto_categorize:
            asyncio.create_task(_auto_categorize_items([item_id], household_id))

        return {"id": item_id, "message": "Item added"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in create_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in create_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/shopping/{item_id}")
async def update_shopping_item(item_id: int, request: Request, tenant: TenantContext = Depends(get_current_user)):
    """Update a shopping item."""
    household_id = tenant.household_id
    try:
        data = await request.json()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM shopping_items WHERE id = ? AND household_id = ?",
                (item_id, household_id)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Item not found")

            # --- Input validation ---
            name = data.get('name', existing['name'])
            if isinstance(name, str):
                name = name.strip()
            if not name:
                logger.warning("Validation failed in update_shopping_item: %s", "Item name is required")
                raise HTTPException(status_code=400, detail="Item name is required")
            if isinstance(name, str) and len(name) > 200:
                logger.warning("Validation failed in update_shopping_item: %s", "Item name is too long")
                raise HTTPException(status_code=400, detail="Item name is too long (max 200 characters)")
            # --- End validation ---

            conn.execute("""
                UPDATE shopping_items
                SET name = ?, quantity = ?, category = ?, notes = ?, shop = ?
                WHERE id = ? AND household_id = ?
            """, (
                name,
                data.get('quantity', existing['quantity']),
                data.get('category', existing['category']),
                data.get('notes', existing['notes']),
                data.get('shop', existing['shop'] if 'shop' in existing.keys() else ''),
                item_id,
                household_id,
            ))
            conn.commit()

        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Item updated"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in update_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in update_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/shopping/purchased")
async def clear_purchased(tenant: TenantContext = Depends(get_current_user)):
    """Delete all purchased shopping items."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM shopping_items WHERE purchased = 1 AND household_id = ?",
                (household_id,)
            )
            conn.commit()
        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Purchased items cleared"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in clear_purchased: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in clear_purchased: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/shopping/all")
async def clear_all_shopping(tenant: TenantContext = Depends(get_current_user)):
    """Delete all shopping items (clear entire list)."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            conn.execute(
                "DELETE FROM shopping_items WHERE household_id = ?",
                (household_id,)
            )
            conn.commit()
        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Shopping list cleared"}
    except sqlite3.Error as e:
        logger.error("Database error in clear_all_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in clear_all_shopping: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/api/shopping/{item_id}")
async def delete_shopping_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Delete a shopping item."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute(
                "DELETE FROM shopping_items WHERE id = ? AND household_id = ?",
                (item_id, household_id)
            )
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Item deleted"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in delete_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in delete_shopping_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/shopping/{item_id}/purchase")
async def purchase_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Mark a shopping item as purchased."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("""
                UPDATE shopping_items SET purchased = 1, purchased_at = ?
                WHERE id = ? AND household_id = ?
            """, (datetime.now().isoformat(), item_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Item purchased"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in purchase_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in purchase_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/shopping/{item_id}/unpurchase")
async def unpurchase_item(item_id: int, tenant: TenantContext = Depends(get_current_user)):
    """Mark a shopping item as not purchased."""
    household_id = tenant.household_id
    try:
        with get_db() as conn:
            result = conn.execute("""
                UPDATE shopping_items SET purchased = 0, purchased_at = NULL
                WHERE id = ? AND household_id = ?
            """, (item_id, household_id))
            conn.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
        await manager.broadcast({"type": "shopping_updated"}, household_id=household_id)
        return {"message": "Item unpurchased"}
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.error("Database error in unpurchase_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.critical("Unexpected error in unpurchase_item: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/shopping/barcode/{code}")
async def barcode_lookup(code: str, tenant: TenantContext = Depends(get_current_user)):
    """Look up a product by barcode using multiple databases (AU-prioritised)."""
    # Try Open Food Facts first (AU endpoint for better Australian product coverage)
    result = await _lookup_openfoodfacts(code)
    if result:
        return {**result, "barcode": code}

    # Fallback to UPCitemdb (large global database, good Australian coverage)
    result = await _lookup_upcitemdb(code)
    if result:
        return {**result, "barcode": code}

    return {"found": False, "barcode": code}


async def _lookup_openfoodfacts(code: str) -> dict | None:
    """Try Open Food Facts (AU-prioritised, then global fallback)."""
    for subdomain in ["au", "world"]:
        try:
            url = f"https://{subdomain}.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,brands,quantity,categories_tags"
            req = urllib.request.Request(url, headers={"User-Agent": "Huddle/1.0"})
            try:
                raw = await asyncio.to_thread(lambda u=url, r=req: urllib.request.urlopen(r, timeout=8).read())
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            data = json.loads(raw)
            if data.get("status") != 1 or not data.get("product"):
                continue
            product = data["product"]
            name = product.get("product_name", "")
            if not name:
                continue
            brand = product.get("brands", "")
            qty = product.get("quantity", "")
            if brand and name and brand.lower() not in name.lower():
                name = f"{brand} {name}"
            cat_tags = product.get("categories_tags", [])
            category = _map_openfoodfacts_category(cat_tags)
            return {"found": True, "name": name.strip(), "quantity": qty, "category": category}
        except Exception as e:
            logger.warning("Open Food Facts (%s) lookup failed for %s: %s", subdomain, code, e)
    return None


async def _lookup_upcitemdb(code: str) -> dict | None:
    """Try UPCitemdb free API (no key required, 100 req/day)."""
    try:
        url = f"https://api.upcitemdb.com/prod/trial/lookup?upc={code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Huddle/1.0",
            "Accept": "application/json",
        })
        try:
            raw = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=8).read())
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None
        data = json.loads(raw)
        items = data.get("items", [])
        if not items:
            return None
        item = items[0]
        name = item.get("title", "")
        if not name:
            return None
        brand = item.get("brand", "")
        if brand and brand.lower() not in name.lower():
            name = f"{brand} {name}"
        category = item.get("category", "")
        mapped_cat = _map_upcitemdb_category(category) if category else "groceries"
        return {"found": True, "name": name.strip(), "quantity": "", "category": mapped_cat}
    except Exception as e:
        logger.warning("UPCitemdb lookup failed for %s: %s", code, e)
        return None


def _map_openfoodfacts_category(tags: list[str]) -> str:
    """Map Open Food Facts category tags to our shopping categories."""
    tag_str = " ".join(tags).lower()
    if any(w in tag_str for w in ["fruit", "vegetable", "salad", "herb", "legume"]):
        return "fruit_veg"
    if any(w in tag_str for w in ["dairy", "milk", "cheese", "yogurt", "egg", "butter", "cream"]):
        return "dairy"
    if any(w in tag_str for w in ["meat", "poultry", "fish", "seafood", "chicken", "beef", "pork"]):
        return "meat"
    if any(w in tag_str for w in ["bread", "bakery", "pastry", "biscuit", "cake"]):
        return "bakery"
    if any(w in tag_str for w in ["frozen"]):
        return "frozen"
    if any(w in tag_str for w in ["beverage", "drink", "juice", "water", "coffee", "tea", "alcohol", "wine", "beer"]):
        return "drinks"
    if any(w in tag_str for w in ["cleaning", "detergent", "soap", "toiletry"]):
        return "cleaning"
    if any(w in tag_str for w in ["cereal", "pasta", "rice", "sauce", "spice", "oil", "flour", "sugar", "canned", "snack", "chocolate", "confectionery", "condiment"]):
        return "pantry"
    return "groceries"


def _map_upcitemdb_category(category: str) -> str:
    """Map UPCitemdb category string to our shopping categories."""
    cat = category.lower()
    if any(w in cat for w in ["fruit", "vegetable", "produce", "salad"]):
        return "fruit_veg"
    if any(w in cat for w in ["dairy", "milk", "cheese", "yogurt", "egg", "butter"]):
        return "dairy"
    if any(w in cat for w in ["meat", "poultry", "fish", "seafood"]):
        return "meat"
    if any(w in cat for w in ["bread", "bakery", "pastry"]):
        return "bakery"
    if any(w in cat for w in ["frozen"]):
        return "frozen"
    if any(w in cat for w in ["beverage", "drink", "juice", "water", "coffee", "tea", "alcohol", "wine", "beer"]):
        return "drinks"
    if any(w in cat for w in ["cleaning", "household", "laundry", "toiletry", "personal care", "health", "beauty"]):
        return "cleaning"
    if any(w in cat for w in ["cereal", "pasta", "sauce", "spice", "canned", "snack", "candy", "chocolate", "condiment", "food"]):
        return "pantry"
    return "groceries"
