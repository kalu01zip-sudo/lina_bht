import random

from app.core.mongo_client import products_collection
from app.services.product_catalog_service import serialize_product_doc


# ==========================================
# FETCH PRODUCT BY CATEGORY
# ==========================================

def fetch_product_by_category(
    category: str
):
    try:
        products = list(products_collection.find(
            {"$or": [{"categories": category}, {"category": category}]}
        ).limit(20))

        if not products:
            return None

        return serialize_product_doc(
            random.choice(products)
        )
    except Exception:
        return None
