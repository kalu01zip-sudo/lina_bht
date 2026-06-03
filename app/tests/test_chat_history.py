# app/tests/test_chat_history.py
import asyncio
import httpx
from main import app
from pymongo import MongoClient
import os
import uuid
from datetime import datetime, timezone, timedelta

# Synchronous DB setup for tests verification
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "skinsense")
sync_client = MongoClient(MONGO_URL)
sync_db = sync_client[DB_NAME]
users_sync_col = sync_db["users"]
chat_messages_sync_col = sync_db["chat_messages"]

async def run_tests_async():
    print("[RUNNING] Initializing Chat History Offset integration tests...")
    
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        test_email = f"test_chat_{uuid.uuid4().hex[:6]}@example.com"
        
        # 1. Sign up test user
        signup_res = await client.post(
            "/auth/signup",
            json={
                "email": test_email,
                "password": "SecurePassword123",
                "full_name": "Test Chat User"
            }
        )
        assert signup_res.status_code == 201, f"Signup failed: {signup_res.text}"
        print("[PASS] Test user signup successful.")

        # Verify email in DB
        users_sync_col.update_one(
            {"email": test_email},
            {"$set": {"is_verified": True}}
        )

        # 2. Sign in test user to get token
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
        print("[PASS] Signin successful, authorization token retrieved.")

        # Find user ID
        user_doc = users_sync_col.find_one({"email": test_email})
        user_id = str(user_doc["_id"])

        # 3. Insert 60 dummy messages
        # Message 1 is the oldest (60 min ago), Message 60 is the latest (1 min ago)
        base_time = datetime.now(timezone.utc) - timedelta(hours=2)
        dummy_messages = []
        for i in range(1, 61):
            dummy_messages.append({
                "user_id": user_id,
                "role": "user" if i % 2 == 1 else "assistant",
                "content": f"Message {i:02d}",
                "created_at": base_time + timedelta(minutes=i)
            })
        
        chat_messages_sync_col.insert_many(dummy_messages)
        print(f"[PASS] Inserted 60 test messages for user {user_id}.")

        # 4. Fetch with limit=10, offset=0 (Expect Messages 51 to 60)
        res_offset_0 = await client.get("/chat/history?limit=10&offset=0", headers=headers)
        assert res_offset_0.status_code == 200, f"Failed to get history with offset 0: {res_offset_0.text}"
        body_0 = res_offset_0.json()
        history_0 = body_0["messages"]
        assert body_0["total"] == 60
        assert body_0["limit"] == 10
        assert body_0["offset"] == 0
        assert len(history_0) == 10, f"Expected 10 messages, got {len(history_0)}"
        assert history_0[0]["content"] == "Message 51"
        assert history_0[-1]["content"] == "Message 60"
        print("[PASS] offset=0 returned the latest 10 messages (Message 51 to Message 60) in chronological order.")

        # 5. Fetch with limit=10, offset=10 (Expect Messages 41 to 50)
        res_offset_10 = await client.get("/chat/history?limit=10&offset=10", headers=headers)
        assert res_offset_10.status_code == 200, f"Failed to get history with offset 10: {res_offset_10.text}"
        body_10 = res_offset_10.json()
        history_10 = body_10["messages"]
        assert body_10["total"] == 60
        assert body_10["limit"] == 10
        assert body_10["offset"] == 10
        assert len(history_10) == 10
        assert history_10[0]["content"] == "Message 41"
        assert history_10[-1]["content"] == "Message 50"
        print("[PASS] offset=10 skipped the latest 10 messages and returned Messages 41 to 50.")

        # 6. Fetch with limit=50, offset=50 (Expect Messages 01 to 10)
        res_offset_50 = await client.get("/chat/history?limit=50&offset=50", headers=headers)
        assert res_offset_50.status_code == 200, f"Failed to get history with offset 50: {res_offset_50.text}"
        body_50 = res_offset_50.json()
        history_50 = body_50["messages"]
        assert body_50["total"] == 60
        assert body_50["limit"] == 50
        assert body_50["offset"] == 50
        assert len(history_50) == 10
        assert history_50[0]["content"] == "Message 01"
        assert history_50[-1]["content"] == "Message 10"
        print("[PASS] offset=50 skipped the latest 50 messages and returned the remaining oldest 10 messages.")

        # 7. Clean up
        users_sync_col.delete_one({"_id": user_doc["_id"]})
        chat_messages_sync_col.delete_many({"user_id": user_id})
        print("[PASS] Test data cleaned up successfully.")
        
        print("[SUCCESS] All Chat History Offset tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests_async())
