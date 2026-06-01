import random

from app.core.mongo_client import products_collection


# ==========================================
# FETCH PRODUCT BY CATEGORY
# ==========================================

def fetch_product_by_category(
    category: str
):
    try:
        products = list(products_collection.find(
            {"category": category},
            {"_id": 0}
        ).limit(20))

        if not products:
            return None

        return random.choice(products)
    except Exception:
        return None
