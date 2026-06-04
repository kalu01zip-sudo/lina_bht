import asyncio
import logging

from app.services.product_scan_history import (
    get_product_scan_by_id
)

from app.services.scan_history import (
    get_scan_history
)

from app.utils.saved_routine_filter import (
    get_existing_categories,
    filter_steps_against_saved,
)

logger = logging.getLogger(__name__)

from app.services.generate_product_routine_ai import (
    generate_product_routine_ai
)

from app.services.product_routine_history import (

    get_existing_product_routine,

    save_generated_product_routine
)

from app.services.routine_duplicate_checker import (
    check_duplicate_product
)

from app.core.mongo_client import saved_routines_collection

from app.services.routine_product_injector import (
    inject_products_into_routine
)

from app.services.routine_draft_service import (
    register_product_routine_drafts
)

from app.core.mongo_client import db
from bson import ObjectId


# ==========================================
# GET SAVED ROUTINES
# ==========================================

def get_saved_routines(
    user_id: str
):

    try:
        results = list(saved_routines_collection.find(
            {"user_id": user_id},
            {"_id": 0}
        ))
        return results
    except Exception:
        return []


# ==========================================
# MAIN PRODUCT ROUTINE PIPELINE
# ==========================================

async def run_product_routine_pipeline(

    user_id: str,

    product_scan_id: str
):

    # ======================================
    # CACHE CHECK
    # ======================================

    cached = get_existing_product_routine(

        user_id=user_id,

        product_scan_id=
            product_scan_id
    )

    if cached:

        cached["_id"] = str(
            cached["_id"]
        )

        drafts = register_product_routine_drafts(
            user_id=user_id,
            source="product",
            routine_data=cached["routine"],
            scan_id=product_scan_id
        )

        return {

            "cached": True,

            "routine_step_id": [
                draft["routine_id"]
                for draft in drafts
            ],

            "routine":
                cached["routine"]
        }

    # ======================================
    # FETCH PRODUCT SCAN
    # ======================================

    product_scan = get_product_scan_by_id(
        product_scan_id
    )

    if not product_scan:

        raise Exception(
            "Product scan not found"
        )

    product = product_scan.get(
        "product",
        {}
    )

    product_name = product.get(
        "name"
    )

    # ======================================
    # DUPLICATE CHECK
    # ======================================

    duplicate = check_duplicate_product(

        user_id=user_id,

        product_name=product_name
    )

    if duplicate:

        return {

            "duplicate": True,

            "message":
                "Product already exists in saved routine"
        }

    # ======================================
    # PARALLEL DB FETCHES
    # Fetch scan history, saved routines, and user profile concurrently
    # instead of sequentially — saves ~200ms per request.
    # ======================================

    loop = asyncio.get_event_loop()

    scans_future          = loop.run_in_executor(None, lambda: get_scan_history(user_id=user_id, limit=1))
    routines_future       = loop.run_in_executor(None, lambda: get_saved_routines(user_id))
    query_user_id         = ObjectId(user_id) if isinstance(user_id, str) else user_id
    user_profile_future   = loop.run_in_executor(None, lambda: db["users"].find_one({"_id": query_user_id}))
    existing_cats_future  = loop.run_in_executor(None, lambda: get_existing_categories(user_id))

    scans, existing_routines, user_profile, existing_categories = await asyncio.gather(
        scans_future,
        routines_future,
        user_profile_future,
        existing_cats_future,
    )

    latest_face_scan = scans[0] if scans else None

    if not user_profile:
        raise Exception("User not found")

    # ======================================
    # AI GENERATION
    # ======================================

    routine = await generate_product_routine_ai(
        product_scan=product_scan,
        latest_face_scan=latest_face_scan,
        existing_routines=existing_routines,
        existing_categories=existing_categories,
    )

    # Post-filter: remove steps whose category is already saved
    if routine.get("routine") and routine["routine"].get("steps"):
        time_slot = routine["routine"].get("time", "morning")
        routine["routine"]["steps"] = filter_steps_against_saved(
            routine["routine"]["steps"],
            time_slot,
            existing_categories,
        )

    routine = inject_products_into_routine(

        routine_data=routine,
        user_context=user_profile
    )

    drafts = register_product_routine_drafts(
        user_id=user_id,
        source="product",
        routine_data=routine,
        scan_id=product_scan_id
    )

    # ======================================
    # SAVE CACHE
    # ======================================

    save_generated_product_routine(

        user_id=user_id,

        product_scan_id=
            product_scan_id,

        routine_data=routine
    )

    # ======================================
    # FINAL RESPONSE
    # ======================================

    return {

        "cached": False,

        "duplicate": False,

        "routine_step_id": [
            draft["routine_id"]
            for draft in drafts
        ],

        "routine": routine
    }
