from app.core.supabase_client import supabase


def match_product(category: str, focus: str):
    category = category.lower().replace(" ", "_")
    focus = focus.lower().replace(" ", "_")
    res = supabase.table("products") \
        .select("*") \
        .eq("category", category) \
        .contains("concerns", [focus]) \
        .order("priority", desc=False) \
        .limit(1) \
        .execute()

    if res.data:
        return res.data[0]

    # fallback
    res = supabase.table("products") \
        .select("*") \
        .eq("category", category) \
        .limit(1) \
        .execute()

    return res.data[0] if res.data else None

def get_all_categories():
    res = supabase.table("products") \
        .select("category") \
        .execute()

    categories = list(set([p["category"] for p in res.data]))
    return categories

def normalize_category(cat: str):
    mapping = {
        "gel cleanser": "cleanser",
        "foam cleanser": "cleanser",
        "face wash": "cleanser",

        "vitamin c serum": "serum",
        "brightening serum": "serum",

        "oil free moisturizer": "moisturizer",
        "hydrating cream": "moisturizer",

        "spf sunscreen": "sunscreen",
        "broad spectrum sunscreen": "sunscreen"
    }

    return mapping.get(cat.lower(), cat.lower())