from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from typing import Annotated, List
from app.services.face_validation import validate_image
from app.services.face_ai import analyze_face_with_claude, verify_same_person, _assign_condition_phases
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

router = APIRouter(prefix="/scan", tags=["Face Scan"])


async def _generate_and_upload_overlays_scan(
    image_bytes: bytes,
    ai_data: dict,
    user_id: str,
    scan_id: str,
) -> dict:
    """
    Generate overlays for each condition in detected_condition and visible_area.
    Uploads them to S3 and populates the image_url fields.
    """
    from app.utils.overlay_utils import generate_condition_overlay, generate_redness_overlay
    from app.services.overlay_storage import upload_overlay_to_s3
    from app.services.acne_detection import detect_acne_regions, detect_lesion_regions
    import asyncio

    # Conditions that should use the general lesion detector (not acne-specific)
    LESION_CONDITIONS = {"blackheads", "whiteheads", "enlarged pores"}

    # ── Condition overlays ────────────────────────────────────────────────
    for cond in ai_data.get("detected_condition", []):
        if isinstance(cond, dict):
            cond_lower = cond.get("name", "").lower().strip()

            # Acne / Pimples → acne-specific YOLO detector
            if cond_lower == "acne / pimples":
                try:
                    local_regions = detect_acne_regions(image_bytes)
                    if local_regions:
                        cond["regions"] = local_regions
                except Exception as exc:
                    print(f"[ERROR] Local YOLO acne detection failed for condition: {exc}")

            # Blackheads / Whiteheads / Enlarged Pores → general lesion YOLO detector
            elif cond_lower in LESION_CONDITIONS:
                try:
                    local_regions = detect_lesion_regions(image_bytes)
                    if local_regions:
                        cond["regions"] = local_regions
                except Exception as exc:
                    print(f"[ERROR] Local YOLO lesion detection failed for '{cond_lower}': {exc}")

            if cond.get("regions"):
                try:
                    overlay_bytes = await asyncio.to_thread(
                        generate_condition_overlay,
                        image_bytes,
                        cond["name"],
                        cond["regions"],
                    )
                    url = await upload_overlay_to_s3(
                        overlay_bytes, user_id, scan_id, cond["name"],
                    )
                    cond["image_url"] = url
                except Exception as exc:
                    print(f"[ERROR] Overlay failed for '{cond.get('name')}': {exc}")

    # ── Redness / Acne overlay (visible_area) ────────────────────────────
    va = ai_data.get("visible_area")
    if isinstance(va, dict):
        cond_name = va.get("condition", "Redness")
        # If the visible area is acne / pimple, run the local YOLO detector to override regions
        if cond_name.lower() in ("acne", "pimple"):
            try:
                local_regions = detect_acne_regions(image_bytes)
                if local_regions:
                    va["regions"] = local_regions
            except Exception as exc:
                print(f"[ERROR] Local YOLO acne detection failed for visible_area: {exc}")

        if va.get("regions"):
            try:
                if cond_name.lower() in ("redness", "irritation"):
                    overlay_bytes = await asyncio.to_thread(
                        generate_redness_overlay,
                        image_bytes,
                        va["regions"],
                        va.get("score", 0),
                    )
                else:
                    overlay_bytes = await asyncio.to_thread(
                        generate_condition_overlay,
                        image_bytes,
                        cond_name,
                        va["regions"],
                    )
                url = await upload_overlay_to_s3(
                    overlay_bytes, user_id, scan_id, f"visible_{cond_name.lower()}",
                )
                va["image_url"] = url
            except Exception as exc:
                print(f"[ERROR] Area overlay failed for '{va.get('condition')}': {exc}")

    return ai_data


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
        file_bytes = await file.read()  # ✅ read once

        if not file_bytes:
            errors.append({
                "image": i + 1,
                "error": "empty_file"
            })
            continue

        # MediaPipe check removed/bypassed so no images are dropped
        valid_count += 1
        valid_images.append(file_bytes)
        results.append(file.filename)

    if valid_count != 5:
        raise HTTPException(400, "5 valid images are required")

    # ── Optimize Images ───────────────────────────────────────────────────
    # Optimise all 5 images immediately to max_px=1024. This resolves EXIF orientation
    # differences and aligns dimensions for Claude, S3 uploads, local models, and overlays.
    from app.utils.image_utils import optimise_image
    optimised_images = []
    for img_bytes in valid_images:
        opt_bytes, _ = optimise_image(img_bytes, "image/jpeg", max_px=1024)
        optimised_images.append(opt_bytes)
    valid_images = optimised_images

    # ── Identity Check ────────────────────────────────────────────────────
    try:
        identity_check = await verify_same_person(valid_images)
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
    
    uploaded_image_urls = []

    for img in valid_images:   
        url = await upload_scan_image(img, str(current_user["_id"]))
        if url:
            uploaded_image_urls.append(url)

    # Fetch allowed conditions dynamically
    allowed_conditions = fetch_all_detected_conditions()

    # ── PRE-SCAN: Run local models BEFORE Claude to gather condition hints ────
    yolo_hints: list[str] = []
    pre_scan_acne_regions: list[dict] = []
    pre_scan_lesion_regions: list[dict] = []

    try:
        from app.services.acne_detection import detect_acne_regions, detect_lesion_regions
        from app.services.skin_signals import get_specialized_model_hints

        # Run YOLO on the first image (fastest, most frontal)
        pre_scan_acne_regions = await asyncio.to_thread(detect_acne_regions, valid_images[0])
        if len(pre_scan_acne_regions) >= 2:
            yolo_hints.append(f"acne (YOLO detected {len(pre_scan_acne_regions)} acne lesions with high confidence)")
            print(f"[PRE-SCAN] YOLO acne detected: {len(pre_scan_acne_regions)} regions")

        pre_scan_lesion_regions = await asyncio.to_thread(detect_lesion_regions, valid_images[0])
        lesion_names = {det.get("class_name", "").lower() for det in pre_scan_lesion_regions}
        if "blackhead" in lesion_names or "comedone" in lesion_names:
            yolo_hints.append(f"blackheads (YOLO lesion model detected {len(pre_scan_lesion_regions)} lesions)")
        if "whitehead" in lesion_names:
            yolo_hints.append(f"whiteheads (YOLO lesion model detected whiteheads)")

        # Run specialized signal models for condition hints
        specialized_hints = await asyncio.to_thread(get_specialized_model_hints, valid_images[0])
        yolo_hints.extend(specialized_hints)
        if specialized_hints:
            print(f"[PRE-SCAN] Specialized model hints: {specialized_hints}")

    except Exception as exc:
        print(f"[PRE-SCAN] Non-fatal pre-scan error: {exc}")

    # Call Claude (with pre-scan hints injected into the prompt)
    try:
        ai_data = await analyze_face_with_claude(valid_images, allowed_conditions, yolo_hints=yolo_hints)
    except Exception as e:
        print("AI ERROR:", str(e))
        raise HTTPException(500, f"AI failed: {str(e)}")

    # ── Run local skin signals model for validated scores ─────────────────
    model_scores: dict = {}
    try:
        from app.services.skin_signals import predict_skin_signals
        model_scores = await asyncio.to_thread(predict_skin_signals, valid_images[0])
        if model_scores:
            print(f"[SKIN SIGNALS] Local model scores: {model_scores}")
            # Blend local model scores into checked_area (70% model, 30% Claude)
            checked = ai_data.get("checked_area", {})
            BLEND_MAP = {
                # model_key → list of checked_area keys it can enhance
                "hydration":  ["hydration"],
                "structure":  ["texture", "pore_size"],
                "elasticity": ["elasticity", "firmness"],
                "sun_damage": ["sun_damage", "uv_damage"],
            }
            for model_key, area_keys in BLEND_MAP.items():
                if model_key in model_scores:
                    local_val = model_scores[model_key]
                    for area_key in area_keys:
                        if area_key in checked:
                            claude_val = checked[area_key]
                            blended = round(0.7 * local_val + 0.3 * claude_val)
                            checked[area_key] = max(0, min(100, blended))
                            print(f"  [BLEND] {area_key}: Claude={claude_val} + Model={local_val:.1f} → {checked[area_key]}")

            # Also blend the top-level hydration field
            if "hydration" in model_scores:
                claude_hydration = ai_data.get("hydration", 60)
                blended_hydration = round(0.7 * model_scores["hydration"] + 0.3 * claude_hydration)
                ai_data["hydration"] = max(0, min(100, blended_hydration))
    except Exception as exc:
        print(f"[WARN] Skin signals model failed (non-fatal): {exc}")

    # Attach raw model scores for transparency
    ai_data["model_scores"] = model_scores

    # ── POST-CLAUDE: inject YOLO-confirmed conditions Claude may have missed ──
    # If YOLO found acne with high confidence but Claude didn't include it,
    # inject it directly so it doesn't get dropped.
    try:
        existing_condition_names = {
            c.get("name", "").lower().replace(" ", "_")
            for c in ai_data.get("detected_condition", [])
            if isinstance(c, dict)
        }

        # Acne injection: if YOLO found >= 3 acne regions but Claude missed it
        if len(pre_scan_acne_regions) >= 3 and "acne" not in existing_condition_names:
            severity = "Moderate" if len(pre_scan_acne_regions) >= 6 else "Mild"
            injected_acne = {
                "name": "acne",
                "note": f"YOLO model detected {len(pre_scan_acne_regions)} active acne lesions on face.",
                "severity": severity,
                "regions": [
                    {"x": r["x"], "y": r["y"], "width": r["width"], "height": r["height"]}
                    for r in pre_scan_acne_regions[:5]  # cap at 5 regions
                ],
                "image_url": None,
            }
            ai_data["detected_condition"].append(injected_acne)
            print(f"[POST-CLAUDE] Injected 'acne' condition from YOLO ({len(pre_scan_acne_regions)} regions)")

        # Lesion injection: blackheads/whiteheads
        if pre_scan_lesion_regions:
            lesion_class_map = {}
            for det in pre_scan_lesion_regions:
                cname = det.get("class_name", "").lower()
                lesion_class_map.setdefault(cname, []).append(det)

            for raw_cls, dets in lesion_class_map.items():
                # Normalize YOLO class names → system condition names
                if "blackhead" in raw_cls or "comedone" in raw_cls:
                    target = "blackheads"
                elif "whitehead" in raw_cls:
                    target = "whiteheads"
                else:
                    continue

                if target not in existing_condition_names and len(dets) >= 2:
                    injected_lesion = {
                        "name": target,
                        "note": f"YOLO lesion model detected {len(dets)} {target} on face.",
                        "severity": "Mild" if len(dets) < 5 else "Moderate",
                        "regions": [
                            {"x": r["x"], "y": r["y"], "width": r["width"], "height": r["height"]}
                            for r in dets[:5]
                        ],
                        "image_url": None,
                    }
                    ai_data["detected_condition"].append(injected_lesion)
                    print(f"[POST-CLAUDE] Injected '{target}' condition from YOLO lesion model")

    except Exception as exc:
        print(f"[POST-CLAUDE] Non-fatal injection error: {exc}")

    # Assign/reassign phase field based on final blended checked_area scores
    ai_data = _assign_condition_phases(ai_data)

    # Extract detected conditions from Claude response
    detected_condition_names = []
    for c in ai_data.get("detected_condition", []):
        cname = c.get("name")
        if cname:
            detected_condition_names.append(cname.strip().lower().replace(" ", "_"))

    # Fetch matching recommendations directly from MongoDB collections based on conditions
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

    # print("[SCAN] NUTRITION IDS:", nutrition_ids)
    print("[SCAN] RECIPES RESULT:", recipe_data)
    print("[SCAN] RECIPES COUNT:", len(recipe_data))

    # print("AI CONDITIONS:", ai_data["detected_condition"])
    # print("MAPPED NUTRITION IDS:", nutrition_ids)

    scan_id = save_scan_result(str(current_user["_id"]), {
        "analysis": ai_data,
        "nutritions": nutrition_data,
        "foods": food_data,
        "recipes": recipe_data,
        "images": uploaded_image_urls 
    })

    # ── Generate & upload overlay images ──────────────────────────────────────
    if scan_id and valid_images:
        try:
            ai_data = await _generate_and_upload_overlays_scan(
                valid_images[0], ai_data, str(current_user["_id"]), scan_id
            )
            from app.core.mongo_client import scan_collection
            from bson import ObjectId
            # Update the stored scan in MongoDB with the updated ai_data containing S3 URLs
            await asyncio.to_thread(
                scan_collection.update_one,
                {"_id": ObjectId(scan_id)},
                {"$set": {"analysis": ai_data}}
            )
        except Exception as exc:
            print(f"[ERROR] Overlay generation/upload failed: {exc}")

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
