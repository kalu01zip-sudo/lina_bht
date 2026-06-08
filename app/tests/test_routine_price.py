# app/tests/test_routine_price.py
import asyncio
import httpx
import uuid
import os
from main import app
from pymongo import MongoClient
from bson import ObjectId
from unittest.mock import patch, AsyncMock

# DB setup for tests verification
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "skinsense")
sync_client = MongoClient(MONGO_URL)
sync_db = sync_client[DB_NAME]
users_sync_col = sync_db["users"]
saved_routines_col = sync_db["saved_routines"]

async def run_tests_async():
    print("[RUNNING] Initializing Routine Price Integration Tests...")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        test_email = f"test_price_{uuid.uuid4().hex[:6]}@example.com"

        # 1. Sign up and verify user
        signup_res = await client.post(
            "/auth/signup",
            json={
                "email": test_email,
                "password": "SecurePassword123",
                "full_name": "Test Routine Price User"
            }
        )
        assert signup_res.status_code == 201, f"Signup failed: {signup_res.text}"
        
        users_sync_col.update_one(
            {"email": test_email},
            {"$set": {"is_verified": True}}
        )
        
        # Sign in
        signin_res = await client.post(
            "/auth/signin",
            json={
                "email": test_email,
                "password": "SecurePassword123"
            }
        )
        assert signin_res.status_code == 200, f"Signin failed: {signin_res.text}"
        token = signin_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        user_doc = users_sync_col.find_one({"email": test_email})
        user_id = str(user_doc["_id"])

        # 2. Test POST /generate/manual-routine
        manual_res = await client.post(
            "/generate/manual-routine",
            json={
                "product_name": "Ultra Hydrating Moisturizer",
                "instruction": "Apply day and night to keep skin moist.",
                "time": "morning"
            },
            headers=headers
        )
        assert manual_res.status_code == 200, f"Manual routine creation failed: {manual_res.text}"
        manual_data = manual_res.json()
        assert "routine" in manual_data
        routine_item = manual_data["routine"]
        assert "price" in routine_item, "Price field missing in manual routine step"
        price_1 = routine_item["price"]
        assert isinstance(price_1, float) or isinstance(price_1, int)
        assert price_1 > 0.0
        step_id = routine_item["id"]
        print(f"[PASS] Manual routine created with price: {price_1}")

        # 3. Test GET /routine/all
        list_res = await client.get("/routine/all", headers=headers)
        assert list_res.status_code == 200, f"Get all routines failed: {list_res.text}"
        list_data = list_res.json()
        assert "total_price" in list_data, "total_price key missing in /routine/all response"
        assert list_data["total_price"] == price_1, f"Expected total_price {price_1}, got {list_data['total_price']}"
        print("[PASS] /routine/all returns correct total_price.")

        # 4. Test PATCH /routine/step/{step_id}/price
        patch_res = await client.patch(
            f"/routine/step/{step_id}/price",
            json={"price": 19.99},
            headers=headers
        )
        assert patch_res.status_code == 200, f"PATCH step price failed: {patch_res.text}"
        patch_data = patch_res.json()
        assert patch_data["price"] == 19.99
        assert patch_data["data"]["price"] == 19.99
        print("[PASS] PATCH price manually updated price successfully.")

        # 5. Test GET /routine/all again to check updated total_price
        list_res_2 = await client.get("/routine/all", headers=headers)
        list_data_2 = list_res_2.json()
        assert list_data_2["total_price"] == 19.99, f"Expected total_price 19.99, got {list_data_2['total_price']}"
        print("[PASS] total_price reflects manual PATCH update.")

        # 6. Test GET /routine/details/{routine_id}
        detail_res = await client.get(f"/routine/details/{step_id}", headers=headers)
        assert detail_res.status_code == 200, f"Get detail failed: {detail_res.text}"
        detail_data = detail_res.json()
        assert "product" in detail_data
        assert "price" in detail_data["product"], "Price field missing in details response product object"
        assert detail_data["product"]["price"] == 19.99
        print("[PASS] GET /routine/details returns correct price in product sub-object.")

        # Clean up database entries
        saved_routines_col.delete_many({"user_id": user_id})
        users_sync_col.delete_one({"_id": user_doc["_id"]})
        print("[PASS] Database cleaned up.")
        print("[SUCCESS] All Routine Price Integration Tests Passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests_async())
