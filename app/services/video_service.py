from app.core.supabase_client import supabase


def fetch_best_video(
    phase: str,
    category: str
):

    response = supabase.table("routine_videos") \
        .select("*") \
        .eq("phase", phase) \
        .eq("product_category", category) \
        .order("priority", desc=False) \
        .limit(1) \
        .execute()

    data = response.data or []

    return data[0] if data else None