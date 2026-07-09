from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from typing import Annotated, List
from app.services.face_validation import validate_image
from app.services.face_ai import generate_narrative_from_youcam, verify_same_person, detect_front_facing_image, _assign_condition_phases
from app.clients.youcam_client import youcam_client
import json
import asyncio
from app.core.mapping import extract_nutrition
from app.core.mongo_client import (
    fetch_all_detected_conditions,
    nutritions_collection,
    foods_collection,
    recipes_collection
)

from app.services.nutrition_service import fetch_nutritions
from app.services.food_service import fetch_foods_by_tags
from app.services.recipe_service import fetch_recipes_by_tags
from app.core.recommender import smart_rank
from app.services.scan_storage import save_scan_result
from app.services.scan_history import get_scan_history, get_scan_by_id, get_all_scans_for_comparison, get_scan_analytics
from app.services.scan_comparison_ai import generate_comparison_message
from app.routers.auth import CurrentUser, users_col
from app.services.image_storage import upload_scan_image
from app.utils.image_utils import optimise_image, resize_to_1080x1350

router = APIRouter(prefix="/scan", tags=["Face Scan"])

@router.post("/face")
async def upload_face_images(
    current_user: CurrentUser,
    images: list[UploadFile] = File(...)
):
    user_id = str(current_user["_id"])
    
    from app.services.usage_limiter import check_usage_limit, record_usage
    user_plan = current_user.get("plan", "free")
    await check_usage_limit(user_id, "face_scan", user_plan)

    if len(images) != 5:
        raise HTTPException(400, "Exactly 5 images required")

    errors = []
    valid_count = 0
    valid_images = []
    results = []

    for i, file in enumerate(images):
        file_bytes = await file.read()

        if not file_bytes:
            errors.append({
                "image": i + 1,
                "error": "empty_file"
            })
            continue

        valid_count += 1
        valid_images.append(file_bytes)
        results.append(file.filename)

    if valid_count != 5:
        raise HTTPException(400, "5 valid images are required")

    # ── Keep original high-res images ─────────────────────────────────────
    original_images = list(valid_images)

    # ── Optimize Images for Identity & General Storage ────────────────────
    optimised_images = []
    for img_bytes in original_images:
        opt_bytes, _ = optimise_image(img_bytes, "image/jpeg", max_px=1024)
        optimised_images.append(opt_bytes)

    # ── Identity Check ────────────────────────────────────────────────────
    try:
        identity_check = await verify_same_person(optimised_images)
        if not identity_check.get("same_person", False):
            raise HTTPException(
                status_code=400, 
                detail=f"Images do not belong to the same person. Reason: {identity_check.get('reason')}"
            )
    except HTTPException:
        raise
    except Exception as e:
        print("IDENTITY CHECK ERROR:", str(e))
        raise HTTPException(500, f"Identity check failed: {str(e)}")
    # ──────────────────────────────────────────────────────────────────────

    # Fetch allowed conditions dynamically
    allowed_conditions = fetch_all_detected_conditions()

    # ── YouCam API Integration ────────────────────────────────────────────
    try:
        # Find the front-facing image using optimized images to save bandwidth
        front_index = await detect_front_facing_image(optimised_images)
        print(f"[SCAN] Front-facing image identified at index {front_index}")
        
        # Grab the HIGH-RES original image for cropping
        high_res_front = original_images[front_index]
        
        # Crop the face directly from the high-res image and resize to 1080x1350
        from app.utils.image_utils import crop_and_resize_face_to_1080x1350
        hd_image_bytes, _ = crop_and_resize_face_to_1080x1350(high_res_front)
        print(f"[SCAN] Resized and cropped front-facing image to HD format")
        
        # Request YouCam analysis
        youcam_results = await youcam_client.analyze_skin(
            hd_image_bytes, 
            file_name="hd_front.jpg", 
            content_type="image/jpeg"
        )
        print(f"[SCAN] YouCam analysis completed successfully")
    except Exception as e:
        print("YOUCAM ERROR:", str(e))
        raise HTTPException(500, f"YouCam analysis failed: {str(e)}")

    # ── Upload images to S3 ───────────────────────────────────────────────
    # Replace the front-facing image with our new HD cropped face image
    final_upload_images = list(optimised_images)
    final_upload_images[front_index] = hd_image_bytes

    # Upload all 5 images concurrently to S3
    upload_tasks = [upload_scan_image(img, str(current_user["_id"]), optimise=False) for img in final_upload_images]
    uploaded_urls = await asyncio.gather(*upload_tasks)
    uploaded_image_urls = [url for url in uploaded_urls if url]

    # ── Claude Narrative Generation ───────────────────────────────────────
    try:
        ai_data = await generate_narrative_from_youcam(youcam_results, allowed_conditions)
    except Exception as e:
        print("AI ERROR:", str(e))
        raise HTTPException(500, f"AI narrative generation failed: {str(e)}")

    # Attempt to extract mask URLs and attach them to detected conditions
    mask_overlays = {}
    youcam_outputs = youcam_results.get("results", {}).get("output", [])
    for item in youcam_outputs:
        m_type = item.get("type", "")
        m_urls = item.get("mask_urls", [])
        if m_urls and isinstance(m_urls, list) and len(m_urls) > 0:
            mask_overlays[m_type] = m_urls[0]

    for cond in ai_data.get("detected_condition", []):
        cond_name = cond.get("name", "").lower().replace(" ", "_")
        
        # Mappings for conditions to YouCam HD types
        if f"hd_{cond_name}" in mask_overlays:
            cond["image_url"] = mask_overlays[f"hd_{cond_name}"]
        elif cond_name in mask_overlays:
            cond["image_url"] = mask_overlays[cond_name]
        elif cond_name == "pores" and "hd_pore" in mask_overlays:
            cond["image_url"] = mask_overlays["hd_pore"]
        elif cond_name == "dark_circles" and "hd_dark_circle" in mask_overlays:
            cond["image_url"] = mask_overlays["hd_dark_circle"]
        elif cond_name == "dehydration" and "hd_moisture" in mask_overlays:
            cond["image_url"] = mask_overlays["hd_moisture"]
        elif cond_name == "eye_bags" and "hd_eye_bag" in mask_overlays:
            cond["image_url"] = mask_overlays["hd_eye_bag"]

    # Remove any condition that failed to map a mask overlay (image_url is null)
    original_conditions = ai_data.get("detected_condition", [])
    filtered_conditions = [c for c in original_conditions if c.get("image_url") is not None]
    ai_data["detected_condition"] = filtered_conditions

    # Assign/reassign phase field
    ai_data = _assign_condition_phases(ai_data)

    # ── Database Fetches ──────────────────────────────────────────────────
    detected_condition_names = []
    for c in ai_data.get("detected_condition", []):
        cname = c.get("name")
        if cname:
            detected_condition_names.append(cname.strip().lower().replace(" ", "_"))

    nutrition_cursor = nutritions_collection.find({"detected_condition": {"$in": detected_condition_names}})
    nutrition_data = []
    for doc in nutrition_cursor:
        doc["_id"] = str(doc["_id"])
        nutrition_data.append(doc)

    nutrition_ids = [n["id"] for n in nutrition_data]

    food_cursor = foods_collection.find({"detected_condition": {"$in": detected_condition_names}})
    raw_foods = []
    for doc in food_cursor:
        doc["_id"] = str(doc["_id"])
        raw_foods.append(doc)

    recipe_cursor = recipes_collection.find({"detected_condition": {"$in": detected_condition_names}})
    raw_recipes = []
    for doc in recipe_cursor:
        doc["_id"] = str(doc["_id"])
        raw_recipes.append(doc)

    food_data = smart_rank(raw_foods, nutrition_ids, ai_data)
    recipe_data = smart_rank(raw_recipes, nutrition_ids, ai_data)

    print("[SCAN] RECIPES RESULT:", recipe_data)
    print("[SCAN] RECIPES COUNT:", len(recipe_data))

    scan_id = save_scan_result(str(current_user["_id"]), {
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data,
        "images": uploaded_image_urls 
    })

    # Record usage log in MongoDB
    await record_usage(user_id, "face_scan")

    # ── Gixy: trigger post-scan alert if severe conditions detected ────────────
    try:
        from app.services.lia_coaching_engine import trigger_post_scan_alert
        user_doc = await users_col().find_one({"_id": current_user["_id"]})
        asyncio.get_event_loop().run_in_executor(
            None,
            trigger_post_scan_alert,
            user_id, user_doc or {}, ai_data,
        )
    except Exception as exc:
        print(f"[Gixy] Post-scan alert trigger failed (non-fatal): {exc}")

    return {
        "scan_id": scan_id,
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data
    }

@router.get("/history")
async def scan_history(
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=200, description="Limit the number of returned scans"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    user_id = str(current_user["_id"])

    from app.core.mongo_client import scan_collection
    total = scan_collection.count_documents({"user_id": user_id})
    data = get_scan_history(user_id, limit=limit, offset=offset)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "scans": data
    }
@router.get("/analytics")
async def scan_analytics(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    return get_scan_analytics(user_id)

@router.get("/{scan_id}")
async def scan_detail(scan_id: str, current_user: CurrentUser):
    data = get_scan_by_id(scan_id)

    if not data:
        raise HTTPException(404, "Scan not found")

    # 🔥 SECURITY CHECK
    if data["user_id"] != str(current_user["_id"]):
        raise HTTPException(403, "Not allowed")

    return data


@router.get("/compare/random")
async def random_compare(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    
    # Fetch scans (oldest first)
    scans = await asyncio.to_thread(get_all_scans_for_comparison, user_id)
    
    if not scans:
        return {}

    first_scan_date = scans[0]["created_at"]
    
    # Group by weeks (relative to first scan)
    weeks = {}
    for s in scans:
        date = s["created_at"]
        # If date is a string (legacy), parse it
        if isinstance(date, str):
            from datetime import datetime
            try:
                date = datetime.fromisoformat(date)
            except Exception:
                continue
                
        days_diff = (date - first_scan_date).days
        week_num = (days_diff // 7) + 1
        # Store/Overwrite so we have the latest scan of that week
        weeks[week_num] = s

    results = {}
    
    # Pairs to compare: (key, week_a, week_b)
    pairs = [
        ("compare_1", 1, 4),
        ("compare_2", 2, 3),
        ("compare_3", 1, 3)
    ]
    
    for key, w1, w2 in pairs:
        if w1 in weeks and w2 in weeks:
            scan1 = weeks[w1]
            scan2 = weeks[w2]
            
            # Generate AI message
            message = await generate_comparison_message(
                scan1.get("analysis", {}),
                scan2.get("analysis", {})
            )
            
            # Get one image for each
            img1 = scan1.get("images", [None])[0] if scan1.get("images") else None
            img2 = scan2.get("images", [None])[0] if scan2.get("images") else None
            
            results[key] = {
                "between": [f"week{w1}", f"week{w2}"],
                f"week_{w1}_image_url": img1,
                f"week_{w2}_image_url": img2,
                "message": message
            }
            
    return results
