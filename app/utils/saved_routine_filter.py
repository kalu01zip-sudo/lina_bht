"""
app/utils/saved_routine_filter.py
───────────────────────────────────
Shared helper: reads the user's saved_routines_collection and returns a
mapping of time-slot → list of normalised product_category strings.

Used by all 4 generate-routine endpoints to:
  1. Tell the AI which categories already exist (so it avoids them).
  2. Hard-filter the generated steps at the builder/pipeline level.

Normalisation rule:  lowercase + strip + replace _ and - with spaces
  "face_moisturizer" → "face moisturizer"
  "Face Moisturizer" → "face moisturizer"   ← match!
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _norm(category: str | None) -> str:
    return (category or "").lower().strip().replace("_", " ").replace("-", " ")


def get_existing_categories(user_id: str) -> dict[str, list[str]]:
    """
    Return saved routine categories per time slot.

    Return shape:
        {
            "morning": ["serum", "face moisturizer"],
            "night":   ["face moisturizer", "sunscreen"],
            "weekly":  [],
        }
    """
    from app.core.mongo_client import saved_routines_collection   # lazy import to avoid circular deps

    result: dict[str, list[str]] = {"morning": [], "night": [], "weekly": []}
    try:
        rows = list(
            saved_routines_collection.find(
                {"user_id": user_id},
                {"_id": 0, "time": 1, "product_category": 1},
            )
        )
        for row in rows:
            slot = (row.get("time") or "").strip().lower()
            if slot == "weekly_care":
                slot = "weekly"
            if slot not in result:
                continue
            cat = _norm(row.get("product_category"))
            if cat and cat not in result[slot]:
                result[slot].append(cat)
    except Exception as exc:
        logger.warning("Could not fetch existing routine categories for user=%s: %s", user_id, exc)
    return result


def filter_steps_against_saved(
    steps: list[dict],
    time_slot: str,
    existing: dict[str, list[str]],
    category_key: str = "category",
) -> list[dict]:
    """
    Remove steps whose category is already saved in the user's routine
    for the given time_slot.

    Args:
        steps:        List of step dicts from the AI.
        time_slot:    The slot these steps belong to (morning/night/weekly).
        existing:     Output of get_existing_categories().
        category_key: Dict key that holds the category string in each step.
                      Defaults to "category" (product/manual routine shape).
    """
    slot = time_slot.lower().replace("weekly_care", "weekly")
    saved_cats = {_norm(c) for c in existing.get(slot, [])}
    seen: set[str] = set()
    result = []
    for step in steps:
        cat = _norm(step.get(category_key))
        if cat and cat in saved_cats:
            logger.info("Filtered step — category '%s' already in saved %s routine", cat, slot)
            continue
        if cat and cat in seen:
            logger.info("Filtered duplicate step — category '%s' seen twice in %s", cat, slot)
            continue
        if cat:
            seen.add(cat)
        result.append(step)
    return result
