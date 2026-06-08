from itertools import product

from fastapi import APIRouter, HTTPException

from app.routers.auth import CurrentUser

from app.services.product_service import (
    fetch_product_by_name
)

from app.services.saved_routine_service import (
    fetch_saved_routine
)

from app.services.generate_ingredients_ai import (
    generate_ingredients_ai
)

from app.core.mongo_client import (
    saved_routines_collection,
    scan_collection,
    scalp_scan_collection,
    nutritions_collection,
    foods_collection,
    recipes_collection
)

from bson import ObjectId
from app.core.recommender import smart_rank

from app.services.video_service import (
    fetch_best_video
)

from app.services.routine_detail_ai import (
    generate_routine_detail_ai
)

from app.services.routine_detail_cache import (
    get_cached_routine_detail,
    save_cached_routine_detail
)

# Recommendation fetching migrated to direct MongoDB queries by detected condition


router = APIRouter(
    prefix="/routine",
    tags=["Routine"]
)

from app.utils.price_helper import get_or_generate_price


@router.get("/details/{routine_id}")
async def get_routine_detail(
    routine_id: str,
    current_user: CurrentUser
):

    # =========================
    # CACHE CHECK
    # =========================

    cached = get_cached_routine_detail(routine_id)

    if cached:

        cached["_id"] = str(cached["_id"])

        return cached["generated_data"]

    # =========================
    # FETCH ROUTINE
    # =========================

    routine = fetch_saved_routine(routine_id)

    if not routine:
        raise HTTPException(404, "Routine not found")

    # security
    if routine["user_id"] != str(current_user["_id"]):
        raise HTTPException(403, "Unauthorized")

    # =========================
    # INGREDIENTS (AI on first call, DB from second call)
    # =========================

    existing_ingredients = routine.get("ingredients", [])

    if not existing_ingredients:
        # First time — generate via AI and persist
        ai_ingredients = await generate_ingredients_ai(
            product_name=routine.get("product_name", ""),
            product_category=routine.get("product_category", "")
        )

        if ai_ingredients:
            saved_routines_collection.update_one(
                {"id": routine_id},
                {"$set": {"ingredients": ai_ingredients}}
            )
            routine["ingredients"] = ai_ingredients

    # =========================
    # VIDEO
    # =========================

    video = fetch_best_video(
        routine["phase"],
        routine["product_category"]
    )

    # =========================
    # RECONSTRUCT DETECTED CONDITIONS
    # =========================

    detected_condition_names = []
    scan_analysis = {}

    scan_id = routine.get("scan_id")
    user_id = routine.get("user_id") or str(current_user["_id"])
    scan_doc = None

    if scan_id:
        try:
            scan_doc = scan_collection.find_one({"_id": ObjectId(scan_id)})
            if not scan_doc:
                scan_doc = scalp_scan_collection.find_one({"_id": ObjectId(scan_id)})
        except Exception:
            pass

    # Fallback 1: Latest user scan
    if not scan_doc:
        try:
            latest_face = scan_collection.find_one(
                {"user_id": user_id},
                sort=[("created_at", -1)]
            )
            latest_scalp = scalp_scan_collection.find_one(
                {"user_id": user_id},
                sort=[("created_at", -1)]
            )
            if latest_face and latest_scalp:
                face_time = latest_face.get("created_at")
                scalp_time = latest_scalp.get("created_at")
                scan_doc = latest_face if face_time > scalp_time else latest_scalp
            else:
                scan_doc = latest_face or latest_scalp
        except Exception:
            pass

    if scan_doc:
        scan_analysis = scan_doc.get("analysis", {})
        for c in scan_analysis.get("detected_condition", []):
            if isinstance(c, dict):
                cname = c.get("name")
            else:
                cname = str(c)
            if cname:
                detected_condition_names.append(cname.strip().lower().replace(" ", "_"))

    # Fallback 2: Onboarding profile concerns
    if not detected_condition_names:
        skin_concerns = current_user.get("skin_concerns") or []
        hair_concerns = current_user.get("hair_concerns") or []
        for concern in skin_concerns + hair_concerns:
            if concern:
                detected_condition_names.append(concern.strip().lower().replace(" ", "_"))

    # Fallback 3: Product tags
    if not detected_condition_names:
        try:
            product = fetch_product_by_name(routine.get("product_name", ""))
            if product:
                for tag in product.get("tags") or []:
                    if tag:
                        detected_condition_names.append(tag.strip().lower().replace(" ", "_"))
        except Exception:
            pass

    # =========================
    # FETCH DATA BY CONDITION
    # =========================

    nutritions = []
    foods = []
    recipes = []

    if detected_condition_names:
        try:
            # Fetch nutritions matching detected conditions
            nutrition_cursor = nutritions_collection.find({"detected_condition": {"$in": detected_condition_names}})
            for doc in nutrition_cursor:
                doc["_id"] = str(doc["_id"])
                nutritions.append(doc)

            nutrition_ids = [n["id"] for n in nutritions]

            # Fetch foods matching detected conditions
            food_cursor = foods_collection.find({"detected_condition": {"$in": detected_condition_names}})
            raw_foods = []
            for doc in food_cursor:
                doc["_id"] = str(doc["_id"])
                raw_foods.append(doc)

            # Fetch recipes matching detected conditions
            recipe_cursor = recipes_collection.find({"detected_condition": {"$in": detected_condition_names}})
            raw_recipes = []
            for doc in recipe_cursor:
                doc["_id"] = str(doc["_id"])
                raw_recipes.append(doc)

            # Smart rank based on context
            foods = smart_rank(raw_foods, nutrition_ids, scan_analysis)
            recipes = smart_rank(raw_recipes, nutrition_ids, scan_analysis)

            # User profile allergy filtering
            allergies = [a.lower().strip() for a in current_user.get("allergies") or [] if a]
            if allergies:
                filtered_foods = []
                for item in foods:
                    ingredients = [i.lower() for i in item.get("ingredients") or []]
                    name_lower = item.get("name", "").lower()
                    if not any(allergen in ingredients or allergen in name_lower for allergen in allergies):
                        filtered_foods.append(item)
                foods = filtered_foods

                filtered_recipes = []
                for item in recipes:
                    ingredients = [i.lower() for i in item.get("main_ingredients") or []]
                    name_lower = item.get("name", "").lower()
                    if not any(allergen in ingredients or allergen in name_lower for allergen in allergies):
                        filtered_recipes.append(item)
                recipes = filtered_recipes
        except Exception as e:
            print("[ERROR] Recommendation mapping failed:", e)

    # =========================
    # AI CONTENT
    # =========================

    ai_data = await generate_routine_detail_ai(routine)

    # =========================
    # FINAL RESPONSE
    # =========================

    result = {

        "video_url": video["video_url"] if video else None,

        "video_title": video["title"] if video else None,

        "reading_duration": ai_data["reading_duration"],

        "product": {

            "image_url": routine["product_url"],

            "category": routine["product_category"],

            "name": routine["product_name"],

            "ingredients": routine.get("ingredients", []),

            "price": routine.get("price") if routine.get("price") is not None else get_or_generate_price(routine.get("product_name"), routine.get("product_category"))
        },

        "text": ai_data["text"],

        "key_benefits": ai_data["key_benefits"],

        "what_you_learn": ai_data["what_you_learn"],

        "nutritions": nutritions,

        "foods": foods,

        "recipes": recipes
    }

    # =========================
    # SAVE CACHE
    # =========================

    save_cached_routine_detail(
        routine_id,
        str(current_user["_id"]),
        result
    )

    return result