from app.core.mongo_client import saved_routines_collection

from app.services.generate_manual_routine_ai import (
    generate_manual_routine_ai
)

from app.services.product_catalog_service import (
    find_product_by_name
)

from app.services.product_ranker import (
    fetch_best_product
)

from app.services.product_routine_pipeline import (
    get_saved_routines
)

from app.services.scan_history import (
    get_scan_history
)

from app.services.routine_draft_service import (
    new_routine_id,
    register_product_routine_drafts
)


VALID_TIMES = {
    "morning",
    "night",
    "weekly"
}


CATEGORY_KEYWORDS = {
    "cleanser": [
        "cleanser",
        "face wash",
        "wash",
        "foam"
    ],
    "toner": [
        "toner",
        "mist"
    ],
    "serum": [
        "serum",
        "retinol",
        "niacinamide",
        "vitamin c",
        "aha",
        "bha"
    ],
    "moisturizer": [
        "moisturizer",
        "moisturiser",
        "cream",
        "lotion"
    ],
    "sunscreen": [
        "sunscreen",
        "spf",
        "sunblock"
    ],
    "mask": [
        "mask",
        "clay"
    ],
    "eye_care": [
        "eye"
    ],
    "oil": [
        "oil"
    ],
    "shampoo": [
        "shampoo"
    ],
    "conditioner": [
        "conditioner"
    ]
}


# ==========================================
# NORMALIZATION HELPERS
# ==========================================

def normalize_time(
    time: str
):

    value = (time or "").strip().lower()

    if value not in VALID_TIMES:

        raise ValueError(
            "time must be morning, night, or weekly"
        )

    return value


def infer_category(
    product_name: str,

    catalog_product: dict = None
):

    if catalog_product and catalog_product.get(
        "category"
    ):

        return catalog_product["category"]

    name = (product_name or "").lower()

    for category, keywords in CATEGORY_KEYWORDS.items():

        for keyword in keywords:

            if keyword in name:

                return category

    return "product"


def user_concerns(
    user_profile: dict
):

    concerns = []

    for key in (
        "user_concerns",
        "skin_concerns",
        "hair_concerns"
    ):

        for item in user_profile.get(key, []) or []:

            if item not in concerns:

                concerns.append(item)

    return concerns


def fallback_category(

    time: str,

    manual_category: str
):

    if time == "morning" and manual_category != "sunscreen":

        return "sunscreen"

    if manual_category != "moisturizer":

        return "moisturizer"

    return "cleanser"


# ==========================================
# MANUAL PRODUCT CONTEXT
# ==========================================

def build_manual_product(

    product_name: str,

    instruction: str,

    time: str
):

    catalog_product = find_product_by_name(
        product_name
    )

    category = infer_category(
        product_name=product_name,
        catalog_product=catalog_product
    )

    return {
        "name": product_name.strip(),
        "instruction": instruction.strip(),
        "time": normalize_time(time),
        "category": category,
        "product_id": (
            catalog_product.get("id")
            if catalog_product
            else None
        ),
        "product_url": (
            catalog_product.get("image_url")
            if catalog_product
            else None
        )
    }


# ==========================================
# SIMPLE MANUAL SAVE
# ==========================================

def save_simple_manual_routine(

    user_id: str,

    product_name: str,

    instruction: str,

    time: str
):

    manual_product = build_manual_product(

        product_name=product_name,

        instruction=instruction,

        time=time
    )

    row = {
        "id": new_routine_id(),
        "user_id": user_id,
        "scan_id": "manual",
        "time": manual_product["time"],
        "phase": "maintenance",
        "product_category": manual_product["category"],
        "product_name": manual_product["name"],
        "product_url": manual_product.get(
            "product_url"
        ),
        "why": manual_product["instruction"]
    }

    saved_routines_collection.insert_one({**row})

    saved = row

    return {
        "saved": True,
        "routine_step_id": saved.get(
            "id",
            row["id"]
        ),
        "routine": saved
    }


# ==========================================
# AI ROUTINE INJECTION
# ==========================================

def ensure_manual_step(

    routine_data: dict,

    manual_product: dict
):

    routine = routine_data.setdefault(
        "routine",
        {}
    )

    routine["time"] = manual_product["time"]

    steps = routine.setdefault(
        "steps",
        []
    )

    has_manual = any(
        step.get("source") == "manual"
        or step.get("product_name") == manual_product["name"]
        for step in steps
    )

    if not has_manual:

        steps.insert(
            0,
            {
                "step": 1,
                "source": "manual",
                "category": manual_product["category"],
                "product_name": manual_product["name"],
                "usage_reason": manual_product["instruction"]
            }
        )

    if len(steps) < 2:

        steps.append({
            "step": len(steps) + 1,
            "source": "catalog",
            "category": fallback_category(
                manual_product["time"],
                manual_product["category"]
            ),
            "usage_reason":
                "Complements the manual product while supporting routine balance."
        })

    del steps[4:]

    for index, step in enumerate(
        steps,
        start=1
    ):

        step["step"] = index

    return routine_data


def inject_products(

    routine_data: dict,

    manual_product: dict,

    user_profile: dict
):

    routine_data = ensure_manual_step(
        routine_data=routine_data,
        manual_product=manual_product
    )

    routine = routine_data["routine"]

    enhanced_steps = []

    manual_used = False

    for step in routine.get(
        "steps",
        []
    ):

        category = step.get(
            "category"
        ) or manual_product["category"]

        is_manual = (
            step.get("source") == "manual"
            or step.get("product_name") == manual_product["name"]
            or (
                not manual_used
                and category == manual_product["category"]
            )
        )

        if is_manual:

            manual_used = True

            product_id = manual_product.get(
                "product_id"
            )

            product_name = manual_product["name"]

            product_url = manual_product.get(
                "product_url"
            )

        else:

            product = fetch_best_product(
                category=category,
                user_concerns=user_concerns(
                    user_profile
                ),
                allergies=user_profile.get(
                    "allergies",
                    []
                )
            )

            product_id = (
                product.get("id")
                if product
                else None
            )

            product_name = (
                product.get("name")
                if product
                else None
            )

            product_url = (
                product.get("image_url")
                if product
                else None
            )

        enhanced_steps.append({

            "step":
                step.get("step"),

            "phase":
                routine.get(
                    "phase",
                    "maintenance"
                ),

            "product_id":
                product_id,

            "product_name":
                product_name,

            "product_url":
                product_url,

            "product_category":
                category,

            "usage_reason":
                step.get(
                    "usage_reason"
                ) or manual_product["instruction"]
        })

    routine["steps"] = enhanced_steps

    return routine_data


# ==========================================
# AI MANUAL ROUTINE
# ==========================================

async def generate_manual_ai_routine_draft(

    user_id: str,

    product_name: str,

    instruction: str,

    time: str,

    user_profile: dict
):

    manual_product = build_manual_product(

        product_name=product_name,

        instruction=instruction,

        time=time
    )

    scans = get_scan_history(
        user_id=user_id,
        limit=1
    )

    latest_face_scan = (
        scans[0]
        if scans
        else None
    )

    existing_routines = get_saved_routines(
        user_id
    )

    routine = await generate_manual_routine_ai(
        manual_product=manual_product,
        user_profile=user_profile,
        latest_face_scan=latest_face_scan,
        existing_routines=existing_routines
    )

    routine = inject_products(
        routine_data=routine,
        manual_product=manual_product,
        user_profile=user_profile
    )

    drafts = register_product_routine_drafts(
        user_id=user_id,
        source="manual_ai",
        routine_data=routine,
        scan_id="manual_ai"
    )

    return {
        "saved": False,
        "routine_step_id": [
            draft["routine_id"]
            for draft in drafts
        ],
        "routine": routine
    }
