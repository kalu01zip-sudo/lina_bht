from app.core.supabase_client import supabase


def fetch_saved_routine(routine_id: str):

    response = supabase.table("saved_routines") \
        .select("*") \
        .eq("id", routine_id) \
        .limit(1) \
        .execute()

    data = response.data or []

    return data[0] if data else None