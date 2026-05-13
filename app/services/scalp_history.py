from app.core.mongo_client import scalp_scan_collection
from bson import ObjectId
from datetime import datetime

def save_scalp_scan_result(user_id: str, data: dict):
    doc = {
        "user_id": user_id,
        "analysis": data.get("analysis"),
        "nutritions": data.get("nutritions", []),
        "foods": data.get("foods", []),
        "recipes": data.get("recipes", []),
        "images": data.get("images", []),
        "created_at": datetime.now()
    }
    res = scalp_scan_collection.insert_one(doc)
    return str(res.inserted_id)

def get_scalp_scan_history(user_id: str, limit: int = 10):
    scans = scalp_scan_collection.find(
        {"user_id": user_id},
        {"analysis": 1, "images": 1, "created_at": 1}
    ).sort("created_at", -1).limit(limit)

    result = []
    for s in scans:
        result.append({
            "id": str(s["_id"]),
            "overall_score": s.get("analysis", {}).get("overall_score"),
            "images": s.get("images", []),
            "created_at": s.get("created_at")
        })
    return result

def get_scalp_scan_by_id(scan_id: str):
    try:
        scan = scalp_scan_collection.find_one({"_id": ObjectId(scan_id)})
    except Exception:
        return None
    if not scan: return None
    
    scan["_id"] = str(scan["_id"])
    return scan

def get_all_scalp_scans_for_comparison(user_id: str):
    scans = scalp_scan_collection.find(
        {"user_id": user_id},
        {"analysis": 1, "images": 1, "created_at": 1}
    ).sort("created_at", 1)
    return list(scans)

def get_scalp_analytics(user_id: str):
    scans = get_all_scalp_scans_for_comparison(user_id)
    if not scans:
        return {"improvement_percentage": 0, "weekly_scores": []}

    first_scan_date = scans[0]["created_at"]
    if isinstance(first_scan_date, str):
        try:
            first_scan_date = datetime.fromisoformat(first_scan_date)
        except:
            return {"improvement_percentage": 0, "weekly_scores": []}

    weeks = {}
    for s in scans:
        date = s["created_at"]
        if isinstance(date, str):
            try: date = datetime.fromisoformat(date)
            except: continue
        
        if not isinstance(date, datetime): continue

        days_diff = (date - first_scan_date).days
        week_num = (days_diff // 7) + 1
        
        score = s.get("analysis", {}).get("overall_score", 0)
        weeks[week_num] = score

    weekly_scores = []
    for w in sorted(weeks.keys()):
        weekly_scores.append({"week": f"W{w}", "score": weeks[w]})

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
