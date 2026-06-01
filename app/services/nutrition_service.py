from app.core.mongo_client import nutritions_collection

def fetch_nutritions(nutrition_ids: list[str]):
    if not nutrition_ids:
        return []

    try:
        # Convert any query IDs to lower case
        query_ids = [nid.strip().lower() for nid in nutrition_ids if nid]
        
        cursor = nutritions_collection.find({"id": {"$in": query_ids}})
        results = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            results.append(doc)
        return results

    except Exception as e:
        print("[ERROR] Nutrition fetch error:", e)
        return []