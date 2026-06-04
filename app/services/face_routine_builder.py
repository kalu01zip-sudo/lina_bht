"""
app/services/face_routine_builder.py
──────────────────────────────────────
Maps AI-generated routine plan → enriched steps with real products.

Fixes applied:
  1. Dedup by product_category within each time slot — if the AI returns two
     "serum" steps in the same slot, only the first is kept.
  2. Accepts an optional `excluded_categories` dict  { "morning": {"serum",...}, ... }
     so the router can filter out categories already saved in the user's routine.
"""

import uuid
from app.services.product_matcher import match_product


def _normalise(category: str | None) -> str:
    """
    Normalise a product category string for comparison.
    Converts to lowercase, strips whitespace, and replaces underscores/hyphens
    with spaces so that 'face_moisturizer', 'face-moisturizer', and
    'Face Moisturizer' all become 'face moisturizer' and match correctly.
    """
    return (category or "").lower().strip().replace("_", " ").replace("-", " ")


def build_routine(
    ai_data: dict,
    excluded_categories: dict[str, set[str]] | None = None,
) -> dict:
    """
    Build the final routine response dict.

    Args:
        ai_data:             Raw dict returned by the AI (morning/night/weekly keys).
        excluded_categories: Optional mapping of time-slot → set of lowercase category
                             names that already exist in the user's saved routine.
                             Steps whose category appears here are dropped.
    """
    excluded_categories = excluded_categories or {}

    def enrich_slot(slot_key: str, steps: list[dict]) -> list[dict]:
        """
        Process one time slot:
          1. Drop categories already saved in user's routine for that slot.
          2. Dedup — keep only the first occurrence of each category.
          3. Enrich with real product data.
        """
        excluded = {_normalise(c) for c in excluded_categories.get(slot_key, set())}
        seen_categories: set[str] = set()
        result: list[dict] = []

        for step in steps:
            category = _normalise(step.get("product_category"))

            # Skip if already in the user's saved routine for this slot
            if category and category in excluded:
                continue

            # Dedup within this generated routine
            if category and category in seen_categories:
                continue
            if category:
                seen_categories.add(category)

            focus   = step.get("focus")
            phase   = step.get("phase", "maintenance")
            product = match_product(category, focus) if category else None

            result.append({
                "id":               str(uuid.uuid4()),
                "phase":            phase,
                "product_category": step.get("product_category"),   # keep original casing
                "product_name":     product.get("name")      if product else None,
                "product_url":      product.get("image_url") if product else None,
                "why":              f"Targets {focus} and supports skin balance effectively.",
            })

        return result

    return {
        "why":         ai_data.get("why", []),
        "morning":     enrich_slot("morning",     ai_data.get("morning", [])),
        "night":       enrich_slot("night",        ai_data.get("night", [])),
        "weekly_care": enrich_slot("weekly_care",  ai_data.get("weekly_care", [
            {"phase": "repair", "product_category": "mask", "focus": "hydration"}
        ])),
    }