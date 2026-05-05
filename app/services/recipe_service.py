# app/services/recipe_service.py

from app.core.supabase_client import supabase
from app.core.tag_mapper import expand_tags


def fetch_recipes_by_tags(nutrition_ids: list[str], limit: int = 6):
    if not nutrition_ids:
        return []

    # expand nutrition → include semantic equivalents
    expanded_tags = expand_tags(nutrition_ids)

    try:
        response = supabase.table("recipes") \
            .select("*") \
            .overlaps("tags", expanded_tags) \
            .limit(limit) \
            .execute()

        return response.data or []

    except Exception as e:
        print("❌ Recipe fetch error:", e)
        return []