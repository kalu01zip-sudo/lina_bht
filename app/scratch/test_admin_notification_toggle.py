# app/scratch/test_admin_notification_toggle.py
import asyncio
from bson import ObjectId
from fastapi.testclient import TestClient

from main import app
from app.routers.admin_auth import _get_current_admin
from app.core.mongo_client import lia_notifications_collection

MOCK_ADMIN = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a01"),
    "email": "admin@example.com",
    "full_name": "Test Admin",
    "is_active": True
}

async def mock_get_current_admin():
    return MOCK_ADMIN

def run_tests():
    print("[RUNNING] Initializing Admin Notification Toggle integration tests...")
    app.dependency_overrides[_get_current_admin] = mock_get_current_admin
    client = TestClient(app)

    # 1. Setup a test notification
    notification_id = "test_notif_123"
    notification_id_2 = "test_notif_456"
    lia_notifications_collection.delete_many({"id": {"$in": [notification_id, notification_id_2]}})
    
    lia_notifications_collection.insert_many([
        {
            "id": notification_id,
            "user_id": "test_user_456",
            "trigger": "morning_routine",
            "title": "Morning skin prep",
            "message": "Remember to wash your face!",
            "is_read": False,
            "created_at": "2026-06-07T10:00:00Z"
        },
        {
            "id": notification_id_2,
            "user_id": "test_user_789",
            "trigger": "evening_routine",
            "title": "Evening routine",
            "message": "Time for sleep!",
            "is_read": False,
            "created_at": "2026-06-07T20:00:00Z"
        }
    ])

    print("Initially is_read is False. Toggling to True...")
    
    # 2. Toggle status (should become True)
    resp = client.patch(f"/admin/notification/{notification_id}/toggle-read")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is True
    assert body["is_read"] is True
    
    # Verify in DB
    doc = lia_notifications_collection.find_one({"id": notification_id})
    assert doc is not None
    assert doc["is_read"] is True

    print("Now is_read is True. Toggling back to False...")

    # 3. Toggle status again (should become False)
    resp2 = client.patch(f"/admin/notification/{notification_id}/toggle-read")
    assert resp2.status_code == 200, f"Expected 200, got {resp2.status_code}: {resp2.text}"
    body2 = resp2.json()
    assert body2["success"] is True
    assert body2["is_read"] is False

    # Verify in DB
    doc2 = lia_notifications_collection.find_one({"id": notification_id})
    assert doc2 is not None
    assert doc2["is_read"] is False

    # 4. Try with a non-existent notification ID
    resp_404 = client.patch("/admin/notification/non_existent_id/toggle-read")
    assert resp_404.status_code == 404, f"Expected 404, got {resp_404.status_code}"

    # 5. Test read-all endpoint
    print("Testing read-all endpoint...")
    resp_read_all = client.patch("/admin/notification/read-all")
    assert resp_read_all.status_code == 200, f"Expected 200, got {resp_read_all.status_code}: {resp_read_all.text}"
    body_read_all = resp_read_all.json()
    assert body_read_all["success"] is True
    assert body_read_all["marked_read"] >= 2  # at least the two we inserted

    # Verify both are read in DB
    doc_1 = lia_notifications_collection.find_one({"id": notification_id})
    doc_2 = lia_notifications_collection.find_one({"id": notification_id_2})
    assert doc_1["is_read"] is True
    assert doc_2["is_read"] is True

    # Clean up
    lia_notifications_collection.delete_many({"id": {"$in": [notification_id, notification_id_2]}})
    print("[SUCCESS] Admin Notification Toggle integration tests passed successfully!")

if __name__ == "__main__":
    run_tests()

