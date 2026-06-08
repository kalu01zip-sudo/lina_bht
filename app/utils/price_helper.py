# app/utils/price_helper.py
from app.core.mongo_client import saved_routines_collection, products_collection

def stable_hash(text: str) -> int:
    """A completely deterministic string hashing function (Java String hashCode equivalent)"""
    h = 0
    for char in text:
        h = (31 * h + ord(char)) & 0xFFFFFFFF
    return h

def get_or_generate_price(product_name: str | None, product_category: str | None) -> float:
    """
    Get the price of a product if already saved in the database,
    otherwise generate a deterministic price based on its name/category.
    """
    # 1. Try to find a saved price in the database
    if product_name:
        name_clean = product_name.strip()
        if name_clean:
            # Check if any user has a saved routine step with this product and a price
            saved_doc = saved_routines_collection.find_one(
                {"product_name": name_clean, "price": {"$ne": None}}
            )
            if saved_doc and saved_doc.get("price") is not None:
                try:
                    return round(float(saved_doc["price"]), 2)
                except (ValueError, TypeError):
                    pass

            # Check products collection catalog
            prod_doc = products_collection.find_one(
                {"name": name_clean}
            )
            if prod_doc and prod_doc.get("price") is not None:
                try:
                    return round(float(prod_doc["price"]), 2)
                except (ValueError, TypeError):
                    pass

    # 2. Fallback: generate a realistic, deterministic price using a stable hash
    key = product_name or product_category or "skincare_product"
    key_clean = key.strip().lower()
    
    h = stable_hash(key_clean)
    
    # Realistic skincare product price range: $4.99 to $34.99
    base = 4.99 + (h % 30)
    cents = [0.00, 0.49, 0.99][h % 3]
    return round(base + cents, 2)
