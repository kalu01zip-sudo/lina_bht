from app.core.mongo_client import recipes_collection
from app.core.tag_mapper import expand_tags

def fetch_recipes_by_tags(nutrition_ids: list[str], limit: int = 6):
    if not nutrition_ids:
        return []

    # expand nutrition → include semantic equivalents
    expanded_tags = expand_tags(nutrition_ids)

    try:
        cursor = recipes_collection.find({
            "$or": [
                {"main_ingredients": {"$in": expanded_tags}},
                {"detected_condition": {"$in": expanded_tags}},
                {"tags": {"$in": expanded_tags}}
            ]
        }).limit(limit)
        
        results = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            results.append(doc)
        return results

    except Exception as e:
        print("[ERROR] Recipe fetch error:", e)
        return []