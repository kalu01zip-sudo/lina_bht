from app.core.mongo_client import scan_collection
from bson import ObjectId
from datetime import datetime


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
def get_scan_analytics(user_id: str):
    """
    Calculates weekly skin scores and improvement percentage for analytics graph.
    """
    scans = get_all_scans_for_comparison(user_id)
    
    if not scans:
        return {
            "improvement_percentage": 0,
            "weekly_scores": []
        }

    first_scan_date = scans[0]["created_at"]
    if isinstance(first_scan_date, str):
        try:
            first_scan_date = datetime.fromisoformat(first_scan_date)
        except Exception:
            # Fallback if date parsing fails
            return {
                "improvement_percentage": 0,
                "weekly_scores": []
            }

    weeks = {}
    for s in scans:
        date = s["created_at"]
        if isinstance(date, str):
            try:
                date = datetime.fromisoformat(date)
            except Exception:
                continue
        
        if not isinstance(date, datetime):
            continue

        days_diff = (date - first_scan_date).days
        week_num = (days_diff // 7) + 1
        
        score = s.get("analysis", {}).get("overall_score", 0)
        # Store latest score for that week
        weeks[week_num] = score

    weekly_scores = []
    sorted_weeks = sorted(weeks.keys())
    
    for w in sorted_weeks:
        weekly_scores.append({
            "week": f"W{w}",
            "score": weeks[w]
        })

    # Improvement Calculation: ((last - first) / first) * 100
    improvement_percentage = 0
    if len(weekly_scores) > 1:
        first_score = weekly_scores[0]["score"]
        last_score = weekly_scores[-1]["score"]
        if first_score > 0:
            improvement_percentage = round(((last_score - first_score) / first_score) * 100)
    
    return {
        "improvement_percentage": improvement_percentage,
        "weekly_scores": weekly_scores
    }
