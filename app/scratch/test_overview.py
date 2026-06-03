# app/scratch/test_overview.py
from fastapi.testclient import TestClient
from main import app
from app.routers.admin_auth import _get_current_admin

MOCK_ADMIN = {
    "_id": "admin_test_id",
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

async def mock_get_current_admin():
    return MOCK_ADMIN

def run_tests():
    print("[RUNNING] Initializing Gixy admin overview tests...")

    app.dependency_overrides[_get_current_admin] = mock_get_current_admin
    client = TestClient(app)

    # Call GET /admin/home/overview
    resp = client.get("/admin/home/overview")
    assert resp.status_code == 200, resp.text
    
    body = resp.json()
    assert body["success"] is True
    
    # Check KPIs
    metrics = body["metrics"]
    assert "total_users" in metrics
    assert "active_scans" in metrics
    assert "premium_subs" in metrics
    assert "revenue" in metrics
    
    # Check Weekly Activity
    assert isinstance(body["weekly_activity"], list)
    assert len(body["weekly_activity"]) == 7
    
    # Check Alerts
    assert isinstance(body["recent_alerts"], list)
    assert len(body["recent_alerts"]) > 0
    for a in body["recent_alerts"]:
        assert "title" in a
        assert "description" in a
        assert "time" in a
        assert "type" in a

    print("[SUCCESS] Admin overview tests passed successfully!")

if __name__ == "__main__":
    run_tests()
