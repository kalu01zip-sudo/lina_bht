# app/services/nutrition_service.py

from app.core.supabase_client import supabase


def fetch_nutritions(nutrition_ids: list[str]):
    if not nutrition_ids:
        return []

    try:
        response = supabase.table("nutritions") \
            .select("*") \
            .in_("id", nutrition_ids) \
            .execute()

        return response.data or []

    except Exception as e:
        print("❌ Nutrition fetch error:", e)
        return []