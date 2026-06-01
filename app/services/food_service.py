from app.core.mongo_client import foods_collection
from app.core.tag_mapper import expand_tags

def fetch_foods_by_tags(nutrition_ids: list[str], limit: int = 10):
    if not nutrition_ids:
        return []

    try:
        expanded = expand_tags(nutrition_ids)
        cursor = foods_collection.find({
            "$or": [
                {"ingredients": {"$in": expanded}},
                {"detected_condition": {"$in": expanded}},
                {"tags": {"$in": expanded}}
            ]
        }).limit(limit)
        
        results = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            results.append(doc)
        return results

    except Exception as e:
        print("[ERROR] Food fetch error:", e)
        return []