"""Ollama LLM helper for shopping intelligence (categorization, dedup, typo fixing)."""
import json
import logging
import urllib.request

logger = logging.getLogger("huddle")

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_TIMEOUT = 30

VALID_CATEGORIES = [
    "fruit_veg", "dairy", "meat", "bakery", "pantry",
    "frozen", "drinks", "cleaning", "groceries", "other",
]

CATEGORY_LABELS = {
    "fruit_veg": "Fruit & Veg",
    "dairy": "Dairy & Eggs",
    "meat": "Meat & Seafood",
    "bakery": "Bakery & Bread",
    "pantry": "Pantry & Dry Goods",
    "frozen": "Frozen",
    "drinks": "Drinks & Beverages",
    "cleaning": "Cleaning & Household",
    "groceries": "General Groceries",
    "other": "Other",
}


def categorize_items(item_names: list[str]) -> dict[str, str]:
    """Call Ollama to determine the shopping category for each item.

    Returns a dict mapping item name -> category key.
    Falls back to 'other' for any items that can't be categorized.
    """
    if not item_names:
        return {}

    numbered = "\n".join(f"{i+1}. {name}" for i, name in enumerate(item_names))
    cat_list = ", ".join(VALID_CATEGORIES)

    prompt = (
        "Categorize these shopping list items into exactly one category each.\n"
        f"Valid categories: {cat_list}\n\n"
        "Rules:\n"
        "- fruit_veg: fresh fruit, vegetables, herbs, salad\n"
        "- dairy: milk, cheese, yogurt, eggs, butter, cream\n"
        "- meat: meat, chicken, fish, seafood, deli meats\n"
        "- bakery: bread, rolls, pastries, wraps, tortillas\n"
        "- pantry: canned goods, pasta, rice, sauces, spices, oil, flour, sugar, condiments, nuts, cereal\n"
        "- frozen: frozen meals, ice cream, frozen vegetables\n"
        "- drinks: water, juice, soft drinks, coffee, tea, alcohol\n"
        "- cleaning: cleaning products, laundry, dishwashing, toiletries, paper towels\n"
        "- groceries: items that don't clearly fit other categories\n"
        "- other: non-food items\n\n"
        f"Items:\n{numbered}\n\n"
        "Return ONLY a JSON object mapping each item number to its category key. Example: {\"1\": \"dairy\", \"2\": \"fruit_veg\"}"
    )

    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())

        text = result.get("response", "").strip()
        # Extract JSON from response (may be wrapped in markdown code block)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        mapping = json.loads(text)
        categories = {}
        for i, name in enumerate(item_names):
            cat = mapping.get(str(i + 1), "other")
            if cat not in VALID_CATEGORIES:
                cat = "other"
            categories[name] = cat

        return categories

    except Exception as e:
        logger.warning("Ollama categorization failed: %s", e)
        return {name: "other" for name in item_names}


def find_duplicates(new_items: list[dict], existing_items: list[dict]) -> dict:
    """Use Ollama to fuzzy-match new items against existing shopping list items.

    Args:
        new_items: list of dicts with 'name' key (ingredients to add)
        existing_items: list of dicts with 'id', 'name', 'quantity' keys (current shopping list)

    Returns:
        dict with:
            'matches': {new_item_name: existing_item_id} — items that match an existing item
            'cleaned': {new_item_name: cleaned_name} — typo-fixed names for non-matching items
    """
    if not new_items or not existing_items:
        return {"matches": {}, "cleaned": {}}

    new_names = [item["name"] for item in new_items]
    existing_names = [f"{item['id']}:{item['name']}" for item in existing_items]

    prompt = (
        "You are matching recipe ingredients to an existing shopping list to avoid duplicates.\n\n"
        "EXISTING shopping list items (id:name):\n"
        + "\n".join(f"- {e}" for e in existing_names) + "\n\n"
        "NEW ingredients to add:\n"
        + "\n".join(f"{i+1}. {n}" for i, n in enumerate(new_names)) + "\n\n"
        "For each new ingredient:\n"
        "1. If it matches an existing item (same product, different wording/typo/amount, e.g. 'broccolini bunch' = 'broccolini'), return the existing item's id\n"
        "2. If no match, fix any spelling errors in the ingredient name\n\n"
        'Return ONLY a JSON object: {"matches": {"1": existing_id, ...}, "cleaned": {"2": "corrected name", ...}}\n'
        "- matches: map of new item number to existing item id (only for matches)\n"
        "- cleaned: map of new item number to corrected name (only for non-matches, only if name needs fixing)\n"
    )

    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())

        text = result.get("response", "").strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)

        matches = {}
        for k, v in data.get("matches", {}).items():
            idx = int(k) - 1
            if 0 <= idx < len(new_names):
                # Validate that the existing_id actually exists
                if any(item["id"] == v for item in existing_items):
                    matches[new_names[idx]] = v

        cleaned = {}
        for k, v in data.get("cleaned", {}).items():
            idx = int(k) - 1
            if 0 <= idx < len(new_names) and isinstance(v, str) and v.strip():
                cleaned[new_names[idx]] = v.strip()

        return {"matches": matches, "cleaned": cleaned}

    except Exception as e:
        logger.warning("Ollama dedup matching failed: %s", e)
        return {"matches": {}, "cleaned": {}}
