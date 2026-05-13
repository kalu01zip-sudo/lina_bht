import sys
from datetime import datetime, timedelta

def get_scalp_analytics(scans):
    if not scans:
        return {"improvement_percentage": 0, "weekly_scores": []}

    first_scan_date = scans[0]["created_at"]
    if isinstance(first_scan_date, str):
        first_scan_date = datetime.fromisoformat(first_scan_date)

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

def test_scalp_analytics():
    now = datetime.now()
    mock_scans = [
        {"analysis": {"overall_score": 60}, "created_at": now - timedelta(days=20)}, # W1
        {"analysis": {"overall_score": 65}, "created_at": now - timedelta(days=10)}, # W2
        {"analysis": {"overall_score": 80}, "created_at": now}                       # W3
    ]

    result = get_scalp_analytics(mock_scans)
    print("Scalp Analytics Result:", result)
    assert len(result["weekly_scores"]) == 3
    # (80 - 60) / 60 = 20 / 60 = 33.33% -> 33
    assert result["improvement_percentage"] == 33
    print("Scalp Analytics Test Passed!")

if __name__ == "__main__":
    test_scalp_analytics()
