from datetime import datetime, timedelta, timezone
from app.utils.pregnancy_utils import resolve_pregnancy_phase

def run_tests():
    print("[RUNNING] Running Pregnancy Dynamic Resolution Tests...")

    # Case 1: Just registered (0 days elapsed)
    user_1 = {
        "current_phase": "pregnant",
        "pregnancy_start_month": 5,
        "pregnancy_month_updated_at": datetime.now(timezone.utc)
    }
    resolve_pregnancy_phase(user_1)
    assert user_1["life_phase"] == "pregnant (5)", f"Expected 'pregnant (5)', got '{user_1.get('life_phase')}'"
    print("[PASS] Case 1 Passed: 0 days elapsed -> pregnant (5)")

    # Case 2: 31 days elapsed (should increment by 1)
    user_2 = {
        "current_phase": "pregnant",
        "pregnancy_start_month": 5,
        "pregnancy_month_updated_at": datetime.now(timezone.utc) - timedelta(days=31)
    }
    resolve_pregnancy_phase(user_2)
    assert user_2["life_phase"] == "pregnant (6)", f"Expected 'pregnant (6)', got '{user_2.get('life_phase')}'"
    print("[PASS] Case 2 Passed: 31 days elapsed -> pregnant (6)")

    # Case 3: 155 days elapsed (should increment by 5, capped at 10)
    user_3 = {
        "current_phase": "pregnant",
        "pregnancy_start_month": 5,
        "pregnancy_month_updated_at": datetime.now(timezone.utc) - timedelta(days=155)
    }
    resolve_pregnancy_phase(user_3)
    assert user_3["life_phase"] == "pregnant (10)", f"Expected 'pregnant (10)', got '{user_3.get('life_phase')}'"
    print("[PASS] Case 3 Passed: 155 days elapsed -> pregnant (10)")

    # Case 4: 300 days elapsed (should cap at 10)
    user_4 = {
        "current_phase": "pregnant",
        "pregnancy_start_month": 5,
        "pregnancy_month_updated_at": datetime.now(timezone.utc) - timedelta(days=300)
    }
    resolve_pregnancy_phase(user_4)
    assert user_4["life_phase"] == "pregnant (10)", f"Expected 'pregnant (10)', got '{user_4.get('life_phase')}'"
    print("[PASS] Case 4 Passed: 300 days elapsed -> pregnant (10)")

    # Case 5: String ISO format date handling
    iso_date_str = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
    user_5 = {
        "current_phase": "pregnant",
        "pregnancy_start_month": 5,
        "pregnancy_month_updated_at": iso_date_str
    }
    resolve_pregnancy_phase(user_5)
    assert user_5["life_phase"] == "pregnant (7)", f"Expected 'pregnant (7)', got '{user_5.get('life_phase')}'"
    print("[PASS] Case 5 Passed: ISO string format parsed and resolved correctly -> pregnant (7)")

    print("[SUCCESS] All Pregnancy Dynamic Resolution Tests Passed successfully!")

if __name__ == "__main__":
    run_tests()
