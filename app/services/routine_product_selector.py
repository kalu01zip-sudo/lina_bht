import random

from app.core.supabase_client import (
    supabase
)


# ==========================================
# FETCH PRODUCT BY CATEGORY
# ==========================================

def fetch_product_by_category(

    category: str
):

    response = supabase.table(
        "products"
    ).select("*").eq(
        "category",
        category
    ).limit(20).execute()

    products = response.data

    if not products:

        return None

    return random.choice(products)
