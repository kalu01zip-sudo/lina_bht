from app.services.product_scan_history import (
    get_product_scan_by_id
)

from app.services.scan_history import (
    get_scan_history
)

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

from app.core.supabase_client import (
    supabase
)

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

    response = supabase.table(
        "saved_routines"
    ).select("*").eq(
        "user_id",
        user_id
    ).execute()

    return response.data


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
    # LATEST FACE SCAN
    # ======================================

    scans = get_scan_history(

        user_id=user_id,

        limit=1
    )

    latest_face_scan = (
        scans[0]
        if scans
        else None
    )

    # ======================================
    # SAVED ROUTINES
    # ======================================

    existing_routines = (
        get_saved_routines(
            user_id
        )
    )

    from bson import ObjectId


    # ======================================
    # USER PROFILE
    # ======================================

    query_user_id = user_id

    if isinstance(user_id, str):

        query_user_id = ObjectId(user_id)

    user_profile = db["users"].find_one({

        "_id": query_user_id
    })

    print("USER PROFILE:", user_profile)

    if not user_profile:

        raise Exception(
            "User not found"
        )

    if not user_profile:

        raise Exception(
            "User not found"
        )

    # ======================================
    # AI GENERATION
    # ======================================

    routine = (
        await generate_product_routine_ai(

            product_scan=product_scan,

            latest_face_scan=
                latest_face_scan,

            existing_routines=
                existing_routines
        )
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
