from datetime import datetime, timedelta

def test_week_grouping_logic():
    # Mock scans
    first_date = datetime(2026, 4, 21)
    scans = [
        {"created_at": first_date, "id": "week1_scan"},
        {"created_at": first_date + timedelta(days=2), "id": "week1_scan_latest"}, # same week
        {"created_at": first_date + timedelta(days=8), "id": "week2_scan"},
        {"created_at": first_date + timedelta(days=15), "id": "week3_scan"},
        {"created_at": first_date + timedelta(days=22), "id": "week4_scan"},
    ]
    
    first_scan_date = scans[0]["created_at"]
    weeks = {}
    for s in scans:
        date = s["created_at"]
        days_diff = (date - first_scan_date).days
        week_num = (days_diff // 7) + 1
        weeks[week_num] = s
        
    print(f"Weeks identified: {list(weeks.keys())}")
    assert 1 in weeks and 2 in weeks and 3 in weeks and 4 in weeks
    assert weeks[1]["id"] == "week1_scan_latest"
    assert weeks[4]["id"] == "week4_scan"
    print("Logic test passed!")

if __name__ == "__main__":
    test_week_grouping_logic()
