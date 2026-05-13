import sys
import os
from datetime import datetime, timedelta

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app.services.scan_history import get_scan_analytics
from unittest.mock import patch, MagicMock

def test_analytics():
    user_id = "test_user_123"
    
    # Mock data: 3 scans over 3 weeks
    now = datetime.now()
    mock_scans = [
        {
            "_id": "1",
            "user_id": user_id,
            "analysis": {"overall_score": 45},
            "created_at": now - timedelta(days=15) # Week 1
        },
        {
            "_id": "2",
            "user_id": user_id,
            "analysis": {"overall_score": 50},
            "created_at": now - timedelta(days=8)  # Week 2
        },
        {
            "_id": "3",
            "user_id": user_id,
            "analysis": {"overall_score": 82},
            "created_at": now                       # Week 3
        }
    ]

    with patch("app.services.scan_history.scan_collection") as mock_col:
        # Mock the find().sort() chain
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_scans
        mock_col.find.return_value = mock_cursor
        
        result = get_scan_analytics(user_id)
        
        print("Result:", result)
        
        # Assertions
        assert len(result["weekly_scores"]) == 3
        assert result["weekly_scores"][0]["week"] == "W1"
        assert result["weekly_scores"][0]["score"] == 45
        assert result["weekly_scores"][-1]["score"] == 82
        # (82 - 45) / 45 = 37 / 45 = 0.8222... -> 82%
        assert result["improvement_percentage"] == 82
        print("Test Passed!")

if __name__ == "__main__":
    try:
        test_analytics()
    except Exception as e:
        print(f"Test Failed: {e}")
        import traceback
        traceback.print_exc()
