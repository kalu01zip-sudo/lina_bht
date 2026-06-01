# app/services/food_service.py

from app.core.supabase_client import supabase
from app.core.tag_mapper import expand_tags


def fetch_foods_by_tags(nutrition_ids: list[str], limit: int = 10):
    if not nutrition_ids:
        return []

    try:
        response = supabase.table("foods") \
            .select("*") \
            .overlaps("tags", expand_tags(nutrition_ids)) \
            .limit(limit) \
            .execute()

        return response.data or []

    except Exception as e:
        print("[ERROR] Food fetch error:", e)
        return []