from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import Annotated, List
from app.services.face_validation import validate_image
from app.services.face_ai import analyze_face_with_claude
import json
import asyncio
from app.core.mapping import extract_nutrition
from app.core.mapping import extract_nutrition
from app.services.nutrition_service import fetch_nutritions
from app.services.food_service import fetch_foods_by_tags
from app.services.recipe_service import fetch_recipes_by_tags
from app.core.recommender import smart_rank
from app.services.scan_storage import save_scan_result
from app.services.scan_history import get_scan_history, get_scan_by_id
from fastapi import HTTPException
from app.routers.auth import CurrentUser, users_col
from app.services.image_storage import upload_scan_image

router = APIRouter(prefix="/scan", tags=["Face Scan"])

@router.post("/face")
async def upload_face_images(
    files: list[UploadFile],
    current_user: CurrentUser 
):
    user_id = str(current_user["_id"])
    
    if len(files) != 5:
        raise HTTPException(400, "Exactly 5 images required")

    errors = []
    valid_count = 0
    valid_images = []
    results = []

    for i, file in enumerate(files):
        file_bytes = await file.read()  # ✅ read once

        if not file_bytes:
            errors.append({
                "image": i + 1,
                "error": "empty_file"
            })
            continue

        status = validate_image(file_bytes)

        if status == "ok":
            valid_count += 1
            valid_images.append(file_bytes)   # ✅ reuse later for Claude
            results.append(file.filename)
        else:
            errors.append({
                "image": i + 1,
                "error": status
            })

    # Minimum 3 valid images required
    if valid_count < 3:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Not enough valid images",
                "valid_images": valid_count,
                "errors": errors
            }
        )
    
    uploaded_image_urls = []

    for img in valid_images:   
        url = await upload_scan_image(img, str(current_user["_id"]))
        if url:
            uploaded_image_urls.append(url)

    # Call Claude 
    try:
        ai_data = await analyze_face_with_claude(valid_images)
    except Exception as e:
        print("AI ERROR:", str(e))
        raise HTTPException(500, f"AI failed: {str(e)}")

    nutrition_ids = extract_nutrition(ai_data)

    nutrition_data = fetch_nutritions(nutrition_ids)
    raw_foods = fetch_foods_by_tags(nutrition_ids)
    raw_recipes = fetch_recipes_by_tags(nutrition_ids)

    food_data = smart_rank(raw_foods, nutrition_ids, ai_data)
    recipe_data = smart_rank(raw_recipes, nutrition_ids, ai_data)

    # print("🔍 NUTRITION IDS:", nutrition_ids)
    print("🔍 RECIPES RESULT:", recipe_data)
    print("🔍 RECIPES COUNT:", len(recipe_data))

    # print("AI CONDITIONS:", ai_data["detected_condition"])
    # print("MAPPED NUTRITION IDS:", nutrition_ids)

    scan_id = save_scan_result(str(current_user["_id"]), {
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data,
        "images": uploaded_image_urls 
    })

    # ── Lia: trigger post-scan alert if severe conditions detected ────────
    try:
        from app.services.lia_coaching_engine import trigger_post_scan_alert
        user_doc = await users_col().find_one({"_id": current_user["_id"]})
        asyncio.get_event_loop().run_in_executor(
            None,
            trigger_post_scan_alert,
            user_id, user_doc or {}, ai_data,
        )
    except Exception as exc:
        print(f"[Lia] Post-scan alert trigger failed (non-fatal): {exc}")

    return {
        "scan_id": scan_id,
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data
    }

@router.get("/history")
async def scan_history(current_user: CurrentUser):
    user_id = str(current_user["_id"])

    data = get_scan_history(user_id)

    return {
        "total": len(data),
        "scans": data
    }

@router.get("/{scan_id}")
async def scan_detail(scan_id: str, current_user: CurrentUser):
    data = get_scan_by_id(scan_id)

    if not data:
        raise HTTPException(404, "Scan not found")

    # 🔥 SECURITY CHECK
    if data["user_id"] != str(current_user["_id"]):
        raise HTTPException(403, "Not allowed")

    return data