# app/scratch/test_pagination.py
import asyncio
from bson import ObjectId
from fastapi.testclient import TestClient

from main import app
from app.routers.auth import _get_current_user
from app.core.mongo_client import (
    scan_collection,
    scalp_scan_collection,
    db,
    saved_routines_collection,
    lia_notifications_collection
)
product_scan_col = db["product_scan_history"]

MOCK_USER = {
    "_id": ObjectId("60d5ecb8b3901b001f3c3a00"),
    "email": "user@example.com",
    "full_name": "Test User",
    "is_active": True
}

async def mock_get_current_user():
    return MOCK_USER

def run_tests():
    print("[RUNNING] Initializing pagination validation tests...")

    app.dependency_overrides[_get_current_user] = mock_get_current_user
    client = TestClient(app)

    user_id = str(MOCK_USER["_id"])

    # Setup dummy data for testing
    scan_collection.delete_many({"user_id": user_id})
    scalp_scan_collection.delete_many({"user_id": user_id})
    saved_routines_collection.delete_many({"user_id": user_id})
    lia_notifications_collection.delete_many({"user_id": user_id})

    # Insert 3 face scans
    scan_collection.insert_many([
        {"user_id": user_id, "analysis": {"overall_score": 80}, "images": [], "created_at": "2026-06-03T01:00:00"},
        {"user_id": user_id, "analysis": {"overall_score": 85}, "images": [], "created_at": "2026-06-03T02:00:00"},
        {"user_id": user_id, "analysis": {"overall_score": 90}, "images": [], "created_at": "2026-06-03T03:00:00"},
    ])

    # Insert 3 scalp scans
    scalp_scan_collection.insert_many([
        {"user_id": user_id, "analysis": {"overall_score": 70}, "images": [], "created_at": "2026-06-03T01:00:00"},
        {"user_id": user_id, "analysis": {"overall_score": 75}, "images": [], "created_at": "2026-06-03T02:00:00"},
        {"user_id": user_id, "analysis": {"overall_score": 80}, "images": [], "created_at": "2026-06-03T03:00:00"},
    ])

    # Insert 3 product scans
    from datetime import datetime, timedelta
    from app.services.product_scan_history import product_scan_col
    product_scan_col.delete_many({"user_id": user_id})
    product_scan_col.insert_many([
        {"user_id": user_id, "product": {"name": "P1"}, "analysis": {"overall_score": 50}, "created_at": datetime.utcnow() - timedelta(minutes=30)},
        {"user_id": user_id, "product": {"name": "P2"}, "analysis": {"overall_score": 60}, "created_at": datetime.utcnow() - timedelta(minutes=20)},
        {"user_id": user_id, "product": {"name": "P3"}, "analysis": {"overall_score": 70}, "created_at": datetime.utcnow() - timedelta(minutes=10)},
    ])

    # Insert 3 saved routine steps
    saved_routines_collection.insert_many([
        {"id": "r1", "user_id": user_id, "product_name": "Routine 1", "time": "morning", "is_completed": False},
        {"id": "r2", "user_id": user_id, "product_name": "Routine 2", "time": "night", "is_completed": False},
        {"id": "r3", "user_id": user_id, "product_name": "Routine 3", "time": "weekly", "is_completed": False},
    ])

    # Insert 3 notifications
    lia_notifications_collection.insert_many([
        {"id": "n1", "user_id": user_id, "trigger": "t1", "title": "N1", "message": "msg1", "is_read": False, "created_at": "2026-06-03T01:00:00"},
        {"id": "n2", "user_id": user_id, "trigger": "t2", "title": "N2", "message": "msg2", "is_read": False, "created_at": "2026-06-03T02:00:00"},
        {"id": "n3", "user_id": user_id, "trigger": "t3", "title": "N3", "message": "msg3", "is_read": False, "created_at": "2026-06-03T03:00:00"},
    ])

    # --- Test GET /scan/history pagination ---
    print("Testing GET /scan/history pagination...")
    resp = client.get("/scan/history?limit=2&offset=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["scans"]) == 2
    
    # --- Test GET /scan/scalp/history pagination ---
    print("Testing GET /scan/scalp/history pagination...")
    resp = client.get("/scan/scalp/history?limit=1&offset=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["scans"]) == 1

    # --- Test GET /scan/product/history pagination ---
    print("Testing GET /scan/product/history pagination...")
    resp = client.get("/scan/product/history?limit=2&offset=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["history"]) == 2

    # --- Test GET /routine/all pagination ---
    print("Testing GET /routine/all pagination...")
    resp = client.get("/routine/all?limit=2&offset=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["data"]) == 2

    # --- Test GET /lia/notifications pagination ---
    print("Testing GET /lia/notifications pagination...")
    resp = client.get("/lia/notifications?limit=2&offset=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["notifications"]) == 2

    print("[SUCCESS] All pagination validation tests passed successfully!")

if __name__ == "__main__":
    run_tests()
