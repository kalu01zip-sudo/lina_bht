from app.core.mongo_client import scan_collection
from bson import ObjectId


# =========================
# GET SCAN HISTORY (LIST)
# =========================
def get_scan_history(user_id: str, limit: int = 10):
    scans = scan_collection.find(
        {"user_id": user_id},
        {
            "analysis": 1,
            "images": 1,        # 🔥 include images
            "created_at": 1
        }
    ).sort("created_at", -1).limit(limit)

    result = []

    for s in scans:
        result.append({
            "id": str(s["_id"]),
            "overall_score": s.get("analysis", {}).get("overall_score"),
            "images": s.get("images", []),   # 🔥 safe access
            "created_at": s.get("created_at")
        })

    return result


def get_all_scans_for_comparison(user_id: str):
    """
    Fetches all scans for a user, sorted by date (oldest first).
    """
    scans = scan_collection.find(
        {"user_id": user_id},
        {
            "analysis": 1,
            "images": 1,
            "created_at": 1,
            "user_id": 1
        }
    ).sort("created_at", 1)
    
    return list(scans)


# =========================
# GET SINGLE SCAN
# =========================
def get_scan_by_id(scan_id: str):
    try:
        scan = scan_collection.find_one({"_id": ObjectId(scan_id)})
    except Exception:
        return None

    if not scan:
        return None

    # convert ObjectId → string
    scan["_id"] = str(scan["_id"])

    # 🔥 safety (avoid crash if missing fields)
    scan["images"] = scan.get("images", [])
    scan["analysis"] = scan.get("analysis", {})
    scan["nutritions"] = scan.get("nutritions", [])
    scan["foods"] = scan.get("foods", [])
    scan["recipes"] = scan.get("recipes", [])

    return scan