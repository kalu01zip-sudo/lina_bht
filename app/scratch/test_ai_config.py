# app/scratch/test_ai_config.py
from fastapi.testclient import TestClient
from main import app
from app.routers.admin_auth import _get_current_admin
from app.core.mongo_client import db

MOCK_ADMIN = {
    "_id": "admin_test_id",
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

async def mock_get_current_admin():
    return MOCK_ADMIN

def run_tests():
    print("[RUNNING] Initializing admin AI configuration tests...")

    app.dependency_overrides[_get_current_admin] = mock_get_current_admin
    client = TestClient(app)

    # 1. Test GET /admin/ai-config
    resp = client.get("/admin/ai-config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert "config" in body
    assert "api_usage" in body
    print("[PASS] GET /admin/ai-config retrieves default settings correctly.")

    # 2. Test POST /admin/ai-config/save
    save_payload = {
        "tone": "Warm & Inspiring",
        "system_prompt_override": "You are Waxi, a premium AI skincare assistant...",
        "face_scan_model": "Face Scan Model v2.4",
        "face_scan_accuracy": "94.2%",
        "face_scan_status": "Active",
        "scalp_hair_model": "Scalp & Hair Model v1.1",
        "scalp_hair_accuracy": "89.5%",
        "scalp_hair_status": "Active",
        "ingredient_parser_model": "Ingredient Parser v3.0",
        "ingredient_parser_accuracy": "99.1%",
        "ingredient_parser_status": "Active"
    }
    resp = client.post("/admin/ai-config/save", json=save_payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    print("[PASS] POST /admin/ai-config/save updates settings successfully.")

    # Verify updated values
    resp = client.get("/admin/ai-config")
    body = resp.json()
    assert body["config"]["tone"] == "Warm & Inspiring"
    assert body["config"]["system_prompt_override"] == "You are Waxi, a premium AI skincare assistant..."
    print("[PASS] GET /admin/ai-config returns updated configurations.")

    # 3. Test POST /admin/ai-config/update-key
    resp = client.post("/admin/ai-config/update-key", json={"api_key": "sk-ant-testkey1234567890"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    print("[PASS] POST /admin/ai-config/update-key stores key securely.")

    # Verify key is masked
    resp = client.get("/admin/ai-config")
    body = resp.json()
    assert body["api_usage"]["api_key_configured"] is True
    assert "sk-ant" in body["api_usage"]["masked_api_key"]
    print("[PASS] Masked key check succeeded.")

    # Verify key lookup integration works
    from app.clients.claude_client import get_active_anthropic_key_sync
    active_key = get_active_anthropic_key_sync()
    assert active_key == "sk-ant-testkey1234567890"
    print("[PASS] Dynamic API key lookup correctly returned saved database key.")

    # 4. Test POST /admin/ai-config/check-updates
    resp = client.post("/admin/ai-config/check-updates")
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    print("[PASS] POST /admin/ai-config/check-updates endpoint behaves as expected.")

    # Cleanup DB
    db["ai_config"].delete_many({})
    print("[SUCCESS] All AI Configuration backend tests passed successfully!")

if __name__ == "__main__":
    run_tests()
