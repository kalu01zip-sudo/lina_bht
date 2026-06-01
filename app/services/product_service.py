from app.core.mongo_client import products_collection

def fetch_product_by_name(product_name: str):
    try:
        doc = products_collection.find_one({"name": product_name})
        if doc:
            doc["_id"] = str(doc["_id"])
            return doc
        return None
    except Exception as e:
        print("[ERROR] Product fetch error:", e)
        return None