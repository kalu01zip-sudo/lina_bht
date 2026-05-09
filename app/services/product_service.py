from app.core.supabase_client import supabase


def fetch_product_by_name(product_name: str):

    response = supabase.table("products") \
        .select("*") \
        .eq("name", product_name) \
        .limit(1) \
        .execute()

    data = response.data or []

    return data[0] if data else None