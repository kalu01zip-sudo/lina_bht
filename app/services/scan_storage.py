from app.core.mongo_client import scan_collection
from datetime import datetime


def save_scan_result(user_id: str, data: dict):
    doc = {
        "user_id": user_id,
        "analysis": data.get("analysis"),
        "nutritions": data.get("nutritions"),
        "foods": data.get("foods"),
        "recipes": data.get("recipes"),
        "images": data.get("images", []),  # 🔥 ADD
        "created_at": datetime.utcnow()
    }

    result = scan_collection.insert_one(doc)
    return str(result.inserted_id)