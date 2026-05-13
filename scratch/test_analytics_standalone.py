import sys
from datetime import datetime, timedelta

# Mock all the imports that scan_history needs
class MockMongoClient:
    scan_collection = None

mock_mongo = MockMongoClient()

sys.modules["app.core.mongo_client"] = mock_mongo
sys.modules["bson"] = type("bson", (), {"ObjectId": lambda x: x})

# Now define the function locally to test it, OR try to import it with mocks
# Since scan_history imports from app.core.mongo_client, it should work if we mock it before import

def get_all_scans_for_comparison(user_id):
    # This will be mocked in the test
    pass

def get_scan_analytics(user_id, scans):
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
        weeks[week_num] = score

    weekly_scores = []
    sorted_weeks = sorted(weeks.keys())
    
    for w in sorted_weeks:
        weekly_scores.append({
            "week": f"W{w}",
            "score": weeks[w]
        })

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

def test_analytics():
    user_id = "test_user_123"
    now = datetime.now()
    
    # Test Case 1: Multiple scans, multiple weeks
    mock_scans = [
        {
            "analysis": {"overall_score": 45},
            "created_at": now - timedelta(days=15) # Week 1
        },
        {
            "analysis": {"overall_score": 50},
            "created_at": now - timedelta(days=8)  # Week 2
        },
        {
            "analysis": {"overall_score": 82},
            "created_at": now                       # Week 3
        }
    ]

    result = get_scan_analytics(user_id, mock_scans)
    print("Result 1:", result)
    assert len(result["weekly_scores"]) == 3
    assert result["weekly_scores"][0]["week"] == "W1"
    assert result["weekly_scores"][0]["score"] == 45
    assert result["weekly_scores"][-1]["score"] == 82
    assert result["improvement_percentage"] == 82

    # Test Case 2: One scan
    mock_scans_single = [
        {
            "analysis": {"overall_score": 50},
            "created_at": now
        }
    ]
    result2 = get_scan_analytics(user_id, mock_scans_single)
    print("Result 2:", result2)
    assert len(result2["weekly_scores"]) == 1
    assert result2["improvement_percentage"] == 0

    # Test Case 3: Empty scans
    result3 = get_scan_analytics(user_id, [])
    print("Result 3:", result3)
    assert len(result3["weekly_scores"]) == 0
    assert result3["improvement_percentage"] == 0

    print("All Tests Passed!")

if __name__ == "__main__":
    test_analytics()
