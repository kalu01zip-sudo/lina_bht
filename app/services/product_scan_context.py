from app.services.product_scan_history import (
    get_recent_product_scans
)

from app.services.scan_history import (
    get_scan_history
)

from app.services.ingredient_memory import (
    build_ingredient_memory
)

# ==========================================
# BUILD PRODUCT SCAN AI CONTEXT
# ==========================================

def build_product_scan_context(

    user_id: str
):

    # ======================================
    # LAST FACE SCAN
    # ======================================

    face_scans = get_scan_history(
        user_id=user_id,
        limit=1
    )

    latest_face_scan = (
        face_scans[0]
        if face_scans
        else None
    )

    # ======================================
    # LAST 2 MONTHS PRODUCTS
    # ======================================

    recent_products = (
        get_recent_product_scans(
            user_id=user_id,
            months=2
        )
    )

    # ======================================
    # EXTRACT PRODUCT NAMES
    # ======================================

    previous_products = []

    for item in recent_products:

        product = item.get(
            "product",
            {}
        )

        previous_products.append({

            "name":
                product.get("name"),

            "category":
                product.get("category"),

            "ingredients":
                item.get(
                    "detected_ingredients",
                    []
                )
        })

    ingredient_memory = build_ingredient_memory(
        user_id
    )

    return {

        "latest_face_scan":
            latest_face_scan,

        "previous_products":
            previous_products,

        "ingredient_memory":
            ingredient_memory
    }