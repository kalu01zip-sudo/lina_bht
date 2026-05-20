# app/tests/test_onboarding_none.py
import asyncio
import httpx
from main import app
from pymongo import MongoClient
import os
import uuid

# Synchronous DB setup for tests verification
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "skinsense")
sync_client = MongoClient(MONGO_URL)
sync_db = sync_client[DB_NAME]
users_sync_col = sync_db["users"]

async def run_tests_async():
    print("[RUNNING] Initializing Onboarding 'none' Defaults Tests...")
    
    # We use httpx.AsyncClient to make async requests to the FastAPI app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        test_email_male = f"test_male_{uuid.uuid4().hex[:6]}@example.com"
        test_email_female = f"test_female_{uuid.uuid4().hex[:6]}@example.com"
        
        # 1. TEST MALE FLOW
        # Sign up male user
        signup_res_male = await client.post(
            "/auth/signup",
            json={
                "email": test_email_male,
                "password": "SecurePassword123",
                "full_name": "Test Male User"
            }
        )
        assert signup_res_male.status_code == 201, f"Signup failed: {signup_res_male.text}"
        print("[PASS] Male signup response received.")

        # Directly verify email in DB using synchronous pymongo
        users_sync_col.update_one(
            {"email": test_email_male},
            {"$set": {"is_verified": True}}
        )

        # Sign in male user
        signin_res_male = await client.post(
            "/auth/signin",
            json={
                "email": test_email_male,
                "password": "SecurePassword123"
            }
        )
        assert signin_res_male.status_code == 200, f"Signin failed: {signin_res_male.text}"
        token_male = signin_res_male.json()["access_token"]
        headers_male = {"Authorization": f"Bearer {token_male}"}

        # Verify initial signup state has 'none' default for current_phase and life_phase
        user_doc = users_sync_col.find_one({"email": test_email_male})
        assert user_doc.get("current_phase") == "none", f"Expected current_phase 'none', got {user_doc.get('current_phase')}"
        assert user_doc.get("life_phase") == "none", f"Expected life_phase 'none', got {user_doc.get('life_phase')}"
        print("[PASS] Male user defaults to current_phase='none' and life_phase='none' on signup.")

        # Call personal info for male user
        personal_info_res = await client.post(
            "/onboarding/personal_info",
            json={
                "country": "US",
                "language": "en",
                "date_of_birth": "1990-01-01",
                "gender": "male"
            },
            headers=headers_male
        )
        assert personal_info_res.status_code == 200, f"Personal info save failed: {personal_info_res.text}"
        
        # Check DB state for male user
        user_doc = users_sync_col.find_one({"email": test_email_male})
        assert user_doc.get("current_phase") == "none", f"Expected current_phase 'none' after personal info, got {user_doc.get('current_phase')}"
        assert user_doc.get("life_phase") == "none", f"Expected life_phase 'none' after personal info, got {user_doc.get('life_phase')}"
        assert user_doc.get("onboarding_step") == "life_phase", f"Expected onboarding_step 'life_phase', got {user_doc.get('onboarding_step')}"
        print("[PASS] Male user has onboarding_step='life_phase' and phase fields saved as 'none' after personal info.")

        # 2. TEST FEMALE FLOW
        signup_res_female = await client.post(
            "/auth/signup",
            json={
                "email": test_email_female,
                "password": "SecurePassword123",
                "full_name": "Test Female User"
            }
        )
        assert signup_res_female.status_code == 201, f"Signup failed: {signup_res_female.text}"
        
        users_sync_col.update_one(
            {"email": test_email_female},
            {"$set": {"is_verified": True}}
        )

        signin_res_female = await client.post(
            "/auth/signin",
            json={
                "email": test_email_female,
                "password": "SecurePassword123"
            }
        )
        assert signin_res_female.status_code == 200, f"Signin failed: {signin_res_female.text}"
        token_female = signin_res_female.json()["access_token"]
        headers_female = {"Authorization": f"Bearer {token_female}"}

        # Call personal info for female user
        personal_info_res_female = await client.post(
            "/onboarding/personal_info",
            json={
                "country": "US",
                "language": "en",
                "date_of_birth": "1995-05-05",
                "gender": "female"
            },
            headers=headers_female
        )
        assert personal_info_res_female.status_code == 200, f"Personal info save failed: {personal_info_res_female.text}"
        
        # Check DB state for female user (should NOT automatically advance onboarding_step to 'life_phase')
        user_doc_female = users_sync_col.find_one({"email": test_email_female})
        assert user_doc_female.get("onboarding_step") == "personal_info", f"Expected onboarding_step 'personal_info', got {user_doc_female.get('onboarding_step')}"
        print("[PASS] Female user onboarding_step remains 'personal_info' after personal_info.")

        # Call /onboarding/life_phase with "none"
        life_phase_none_res = await client.post(
            "/onboarding/life_phase",
            json={
                "current_phase": "none"
            },
            headers=headers_female
        )
        assert life_phase_none_res.status_code == 200, f"Life phase none save failed: {life_phase_none_res.text}"
        
        # Check DB state for female user
        user_doc_female = users_sync_col.find_one({"email": test_email_female})
        assert user_doc_female.get("current_phase") == "none", f"Expected current_phase 'none', got {user_doc_female.get('current_phase')}"
        assert user_doc_female.get("life_phase") == "none", f"Expected life_phase 'none', got {user_doc_female.get('life_phase')}"
        assert user_doc_female.get("onboarding_step") == "life_phase", f"Expected onboarding_step 'life_phase', got {user_doc_female.get('onboarding_step')}"
        print("[PASS] Female user saves current_phase='none' and onboarding_step='life_phase' when calling life_phase with 'none'.")

        # Clean up test users
        users_sync_col.delete_many({"email": {"$in": [test_email_male, test_email_female]}})
        print("[PASS] Test users cleaned up successfully.")
        
        print("[SUCCESS] All Onboarding 'none' Defaults Tests Passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests_async())
