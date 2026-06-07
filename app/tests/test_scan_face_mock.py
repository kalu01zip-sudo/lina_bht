# app/tests/test_scan_face_mock.py
import asyncio
import httpx
import numpy as np
import cv2
from main import app
from pymongo import MongoClient
import os
import uuid
from unittest.mock import patch, AsyncMock
from bson import ObjectId

# Synchronous DB setup for tests verification
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME", "skinsense")
sync_client = MongoClient(MONGO_URL)
sync_db = sync_client[DB_NAME]
users_sync_col = sync_db["users"]
scan_results_col = sync_db["face_scans"]  # Active face scan collection is face_scans

async def run_tests_async():
    print("[RUNNING] Initializing Face Scan Integration Tests with Mocked LLM...")
    
    # We use httpx.AsyncClient to make async requests to the FastAPI app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        test_email = f"test_face_scan_{uuid.uuid4().hex[:6]}@example.com"
        
        # 1. Sign up user
        signup_res = await client.post(
            "/auth/signup",
            json={
                "email": test_email,
                "password": "SecurePassword123",
                "full_name": "Test Face Scan User"
            }
        )
        assert signup_res.status_code == 201, f"Signup failed: {signup_res.text}"
        print("[PASS] User signup response received.")

        # Directly verify email in DB using synchronous pymongo
        users_sync_col.update_one(
            {"email": test_email},
            {"$set": {"is_verified": True}}
        )
        user_doc = users_sync_col.find_one({"email": test_email})
        user_id = str(user_doc["_id"])

        # Sign in user
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

        # Create 5 valid mock JPEG images (100x100 pixels)
        img = np.ones((100, 100, 3), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".jpg", img)
        valid_jpeg_bytes = buf.tobytes()

        files = [
            ("images", ("img1.jpg", valid_jpeg_bytes, "image/jpeg")),
            ("images", ("img2.jpg", valid_jpeg_bytes, "image/jpeg")),
            ("images", ("img3.jpg", valid_jpeg_bytes, "image/jpeg")),
            ("images", ("img4.jpg", valid_jpeg_bytes, "image/jpeg")),
            ("images", ("img5.jpg", valid_jpeg_bytes, "image/jpeg")),
        ]

        # 2. Mock Claude Vision API calls
        mock_verify = AsyncMock(return_value={"same_person": True})
        mock_analyze = AsyncMock(return_value={
            "overall_score": 75,
            "checked_area": {
                "hydration": 80,
                "sebum": 70,
                "redness": 60,
                "texture": 75,
                "evenness": 80
            },
            "visible_area": {
                "condition": "redness",
                "areas": ["cheeks"],
                "score": 40,
                "regions": [{"x": 0.3, "y": 0.25, "width": 0.4, "height": 0.2}]
            },
            "hydration": 60,
            "detected_condition": [
                {
                    "name": "acne",
                    "note": "Mild acne breakout observed on forehead area.",
                    "severity": "Mild",
                    "regions": [{"x": 0.42, "y": 0.35, "width": 0.08, "height": 0.07}]
                },
                {
                    "name": "blackheads",
                    "note": "Visible blackheads clustered around nose area.",
                    "severity": "Mild",
                    "regions": [{"x": 0.4, "y": 0.5, "width": 0.2, "height": 0.15}]
                }
            ],
            "lifestyle_factor": {
                "stress_score": 30,
                "water_intake": 80,
                "sleep_quality": 70
            },
            "prognosis_timeline": {
                "seven_days": {"hydration": 5},
                "fourteen_days": {"hydration": 10}
            },
            "hydration_target": 2000,
            "model_scores": {},
            "score_breakdown": {
                "baseline": 95,
                "deductions": [
                    {"condition": "acne", "severity": "Mild", "penalty": 8},
                    {"condition": "blackheads", "severity": "Mild", "penalty": 4}
                ],
                "final_score": 83
            }
        })

        # Mock the local ML models
        mock_skin_signals = lambda img_bytes: {"structure": 65.2, "hydration": 72.0, "sun_damage": 18.5, "elasticity": 78.3}
        mock_acne_regions = lambda img_bytes: [{"x": 0.42, "y": 0.35, "width": 0.08, "height": 0.07}]
        mock_lesion_regions = lambda img_bytes: [{"x": 0.4, "y": 0.5, "width": 0.2, "height": 0.15}]

        with patch("app.routers.scan.verify_same_person", new=mock_verify), \
             patch("app.routers.scan.analyze_face_with_claude", new=mock_analyze), \
             patch("app.services.skin_signals.predict_skin_signals", side_effect=mock_skin_signals), \
             patch("app.services.acne_detection.detect_acne_regions", side_effect=mock_acne_regions), \
             patch("app.services.acne_detection.detect_lesion_regions", side_effect=mock_lesion_regions):
             
            scan_res = await client.post(
                "/scan/face",
                files=files,
                headers=headers
            )
            
        assert scan_res.status_code == 200, f"Scan failed: {scan_res.text}"
        
        resp_json = scan_res.json()
        print("[PASS] Scan response received:", resp_json.keys())
        
        assert "scan_id" in resp_json, "Response missing scan_id"
        scan_id = resp_json["scan_id"]
        
        analysis = resp_json["analysis"]
        assert analysis["overall_score"] == 75
        
        # Verify region coordinates exist in response
        assert "regions" in analysis["visible_area"]
        assert analysis["visible_area"]["regions"][0]["x"] == 0.3
        assert analysis["visible_area"]["image_url"] is not None
        assert "scan_overlays" in analysis["visible_area"]["image_url"]
        
        # Verify detected conditions have image_url containing S3 paths
        cond = analysis["detected_condition"][0]
        assert cond["name"] == "acne"
        assert cond["regions"][0]["x"] == 0.42
        assert cond["image_url"] is not None
        assert "scan_overlays" in cond["image_url"]
        print("[PASS] Scan response correctly contains overlays S3 URLs and region coordinates.")

        # Verify model_scores field exists
        assert "model_scores" in analysis, "Response missing model_scores"
        ms = analysis["model_scores"]
        assert ms.get("structure") == 65.2, f"Expected structure=65.2, got {ms.get('structure')}"
        assert ms.get("hydration") == 72.0, f"Expected hydration=72.0, got {ms.get('hydration')}"
        assert ms.get("sun_damage") == 18.5
        assert ms.get("elasticity") == 78.3
        print("[PASS] model_scores field present with correct local ML scores.")

        # Verify blackheads condition also got overlay
        blackhead_cond = analysis["detected_condition"][1]
        assert blackhead_cond["name"] == "blackheads"
        assert blackhead_cond.get("image_url") is not None
        print("[PASS] Blackheads condition received lesion detector overlay.")

        # 3. Test Database storage update
        db_doc = scan_results_col.find_one({"_id": ObjectId(scan_id)})
        assert db_doc is not None, "Database record missing"
        db_analysis = db_doc["analysis"]
        assert db_analysis["visible_area"]["image_url"] == analysis["visible_area"]["image_url"]
        assert db_analysis["detected_condition"][0]["image_url"] == cond["image_url"]
        print("[PASS] Database record matches response with updated S3 URLs.")

        # 4. Check scan details retrieval endpoint
        detail_res = await client.get(
            f"/scan/{scan_id}",
            headers=headers
        )
        assert detail_res.status_code == 200, f"Details retrieval failed: {detail_res.text}"
        detail_json = detail_res.json()
        assert detail_json["analysis"]["visible_area"]["image_url"] == analysis["visible_area"]["image_url"]
        print("[PASS] Single scan detail endpoint retrieves updated URLs.")

        # Clean up database entries
        scan_results_col.delete_many({"user_id": user_id})
        users_sync_col.delete_one({"_id": user_doc["_id"]})
        print("[PASS] Database cleaned up.")
        print("[SUCCESS] All Face Scan Mock Endpoint Tests Passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests_async())
