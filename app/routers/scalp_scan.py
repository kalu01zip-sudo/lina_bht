from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from typing import List
from app.services.scalp_ai import analyze_scalp_with_claude, validate_scalp_or_hair_image
from app.services.scalp_history import (
    save_scalp_scan_result, 
    get_scalp_scan_history, 
    get_scalp_scan_by_id, 
    get_scalp_analytics,
    get_all_scalp_scans_for_comparison
)
from app.routers.auth import CurrentUser
from app.services.image_storage import upload_scan_image
import asyncio
from app.core.mapping import extract_nutrition
from app.services.nutrition_service import fetch_nutritions
from app.services.food_service import fetch_foods_by_tags
from app.services.recipe_service import fetch_recipes_by_tags
from app.core.recommender import smart_rank
from app.services.scan_comparison_ai import generate_comparison_message
from datetime import datetime

router = APIRouter(prefix="/scan", tags=["Scalp Scan"])

# The user specifically requested POST /scan/scalp_hair
@router.post("/scalp_hair")
async def upload_scalp_image(
    current_user: CurrentUser,
    file: UploadFile = File(...)
):
    user_id = str(current_user["_id"])
    
    from app.services.usage_limiter import check_usage_limit, record_usage
    user_plan = current_user.get("plan", "free")
    await check_usage_limit(user_id, "scalp_scan", user_plan)

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Empty file")

    # Cheap preflight: reject unrelated images before the full analysis pipeline.
    try:
        validation = await validate_scalp_or_hair_image(file_bytes)
    except Exception as e:
        raise HTTPException(500, f"Scalp image validation failed: {str(e)}")

    if not validation.get("scalp_or_hair_detected"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_SCALP_OR_HAIR",
                "message": "Please upload a clear scalp or hair image.",
                "reason": validation.get("reason"),
            }
        )

    # Call AI
    try:
        ai_data = await analyze_scalp_with_claude(file_bytes)
    except Exception as e:
        raise HTTPException(500, f"Scalp AI failed: {str(e)}")

    # Upload image
    url = await upload_scan_image(file_bytes, user_id)

    # RECOMMENDATIONS
    nutrition_ids = extract_nutrition(ai_data)
    nutrition_data = fetch_nutritions(nutrition_ids)
    raw_foods = fetch_foods_by_tags(nutrition_ids)
    raw_recipes = fetch_recipes_by_tags(nutrition_ids)

    food_data = smart_rank(raw_foods, nutrition_ids, ai_data)
    recipe_data = smart_rank(raw_recipes, nutrition_ids, ai_data)

    # Save to DB
    scan_id = save_scalp_scan_result(user_id, {
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data,
        "images": [url] if url else []
    })

    # Record usage log in MongoDB
    await record_usage(user_id, "scalp_scan")

    return {
        "scan_id": scan_id,
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data
    }

@router.get("/scalp/history")
async def scalp_history(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    data = get_scalp_scan_history(user_id)
    return {
        "total": len(data),
        "scans": data
    }

@router.get("/scalp/analytics")
async def scalp_analytics(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    return get_scalp_analytics(user_id)

@router.get("/scalp/compare/random")
async def random_compare_scalp(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    scans = await asyncio.to_thread(get_all_scalp_scans_for_comparison, user_id)
    if not scans or len(scans) < 2:
        return {}

    import random
    import itertools
    
    all_indices = list(range(len(scans)))
    # Generate all possible unique pairs of indices
    possible_pairs = list(itertools.combinations(all_indices, 2))
    
    # Randomly shuffle and pick up to 3
    random.shuffle(possible_pairs)
    selected_pairs = possible_pairs[:3]
    
    results = {}
    
    # Calculate approximate weeks relative to the first scan ever
    first_date = scans[0]["created_at"]
    if isinstance(first_date, str):
        first_date = datetime.fromisoformat(first_date)

    def get_week(dt, start_dt):
        if isinstance(dt, str): dt = datetime.fromisoformat(dt)
        # Ensure start_dt is datetime
        if isinstance(start_dt, str): start_dt = datetime.fromisoformat(start_dt)
        days = (dt - start_dt).days
        return (days // 7) + 1

    for i, (idx1, idx2) in enumerate(selected_pairs, 1):
        # Sort indices to ensure scan1 is older than scan2
        s1_idx, s2_idx = (idx1, idx2) if idx1 < idx2 else (idx2, idx1)
        scan1 = scans[s1_idx]
        scan2 = scans[s2_idx]
        
        w1 = get_week(scan1["created_at"], first_date)
        w2 = get_week(scan2["created_at"], first_date)

        message = await generate_comparison_message(
            scan1.get("analysis", {}),
            scan2.get("analysis", {})
        )

        results[f"compare_{i}"] = {
            "between": [f"week{w1}", f"week{w2}"],
            f"week_{w1}_image_url": scan1.get("images", [None])[0] if scan1.get("images") else None,
            f"week_{w2}_image_url": scan2.get("images", [None])[0] if scan2.get("images") else None,
            "message": message
        }

    return results

@router.get("/scalp/{scan_id}")
async def scalp_detail(scan_id: str, current_user: CurrentUser):
    data = get_scalp_scan_by_id(scan_id)
    if not data:
        raise HTTPException(404, "Scan not found")
    if data["user_id"] != str(current_user["_id"]):
        raise HTTPException(403, "Not allowed")

    return {
        "scan_id": str(data["_id"]),
        "analysis": data.get("analysis"),
        "nutritions": data.get("nutritions", []),
        "foods": data.get("foods", []),
        "recipes": data.get("recipes", []),
        "images": data.get("images", []),
        "created_at": data.get("created_at")
    }
