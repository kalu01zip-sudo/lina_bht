from app.core.mongo_client import saved_routines_collection

def fetch_saved_routine(routine_id: str):
    try:
        doc = saved_routines_collection.find_one({"id": routine_id})
        if doc:
            doc["_id"] = str(doc["_id"])
            return doc
        return None
    except Exception as e:
        print("[ERROR] Saved routine fetch error:", e)
        return None