from app.core.mongo_client import routine_videos_collection

def fetch_best_video(
    phase: str,
    category: str
):
    try:
        doc = routine_videos_collection.find_one(
            {"phase": phase, "product_category": category},
            sort=[("priority", 1)]
        )
        if doc:
            doc["_id"] = str(doc["_id"])
            return doc
        return None
    except Exception as e:
        print("[ERROR] Best video fetch error:", e)
        return None