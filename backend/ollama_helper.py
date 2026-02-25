"""Lightweight async Ollama helper for simple AI tasks across Huddle.

Uses the local Ollama API (default: http://localhost:11434) for text generation.
Falls back gracefully if Ollama is unavailable.
"""

import logging
from typing import Optional

logger = logging.getLogger("huddle")

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5-coder:3b"
OLLAMA_TIMEOUT = 15.0


async def generate(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.3) -> Optional[str]:
    """Send a prompt to Ollama and return the response text. Returns None on failure."""
    try:
        import httpx
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json=payload,
                timeout=OLLAMA_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", "").strip()
            else:
                logger.warning("Ollama returned status %d", resp.status_code)
                return None
    except Exception as e:
        logger.debug("Ollama unavailable: %s", e)
        return None


async def suggest_recipe_tags(name: str, description: str = "", ingredients: list[str] | None = None) -> list[str]:
    """Suggest category tags for a recipe based on its name, description, and ingredients."""
    valid_tags = ["breakfast", "lunch", "dinner", "snack", "dessert", "vegetarian",
                  "vegan", "gluten-free", "quick", "slow-cooker", "baking", "salad",
                  "soup", "pasta", "seafood", "chicken", "beef", "pork", "asian",
                  "mexican", "italian", "indian", "healthy", "comfort-food", "side-dish"]

    parts = [f"Recipe: {name}"]
    if description:
        parts.append(f"Description: {description}")
    if ingredients:
        parts.append(f"Ingredients: {', '.join(ingredients[:15])}")

    system = (
        "You are a recipe categorizer. Given a recipe, output ONLY a comma-separated list of tags. "
        f"Choose from: {', '.join(valid_tags)}. "
        "Pick 2-5 tags that best describe this recipe. Output ONLY the tags, nothing else."
    )

    result = await generate("\n".join(parts), system=system, max_tokens=80, temperature=0.2)
    if not result:
        return []

    # Parse the response — extract valid tags
    raw_tags = [t.strip().lower().replace(" ", "-") for t in result.replace("\n", ",").split(",")]
    return [t for t in raw_tags if t in valid_tags]
