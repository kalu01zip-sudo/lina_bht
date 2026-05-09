from itertools import product

from fastapi import APIRouter, HTTPException

from app.routers.auth import CurrentUser

from app.services.product_service import (
    fetch_product_by_name
)

from app.services.saved_routine_service import (
    fetch_saved_routine
)

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

from app.services.nutrition_service import (
    fetch_nutritions
)

from app.services.food_service import (
    fetch_foods_by_tags
)

from app.services.recipe_service import (
    fetch_recipes_by_tags
)


router = APIRouter(
    prefix="/routine",
    tags=["Routine Detail"]
)


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
    # VIDEO
    # =========================

    video = fetch_best_video(
        routine["phase"],
        routine["product_category"]
    )

    # =========================
    # NUTRITION IDS
    # =========================

    product = fetch_product_by_name(
        routine["product_name"]
    )

    nutrition_ids = []

    if product:

        nutrition_ids = product.get("tags", [])

    # =========================
    # FETCH DATA
    # =========================

    nutritions = fetch_nutritions(nutrition_ids)

    foods = fetch_foods_by_tags(nutrition_ids)

    recipes = fetch_recipes_by_tags(nutrition_ids)

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

            "name": routine["product_name"]
        },

        "text": ai_data["text"],

        "key_benefits": ai_data["key_benefits"],

        "what_you_learn": ai_data["what_you_learn"],

        "key_nutrients": nutritions,

        "food_recommendation": foods,

        "recipe_recommendation": recipes
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