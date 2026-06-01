from app.core.mongo_client import saved_routines_collection


# ==========================================
# CHECK DUPLICATE PRODUCT
# ==========================================

def check_duplicate_product(
    user_id: str,
    product_name: str
):
    try:
        count = saved_routines_collection.count_documents({
            "user_id": user_id,
            "product_name": product_name
        })
        return count > 0
    except Exception:
        return False