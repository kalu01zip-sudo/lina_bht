from app.services.extract_product_ai import (
    extract_product_data
)

from app.services.product_scan_context import (
    build_product_scan_context
)

from app.services.ingredient_conflict_engine import (
    analyze_ingredient_conflicts
)

from app.services.product_analysis_ai import (
    generate_product_analysis
)

from app.services.ingredient_intelligence_engine import (
    calculate_ingredient_intelligence
)

# ==========================================
# FULL PRODUCT SCAN PIPELINE
# ==========================================

async def run_product_scan_pipeline(

    user_id: str,

    image_bytes: bytes
):

    # ======================================
    # STEP 1
    # AI EXTRACTION
    # ======================================

    extracted = await extract_product_data(
        image_bytes
    )

    # ======================================
    # STEP 2
    # USER CONTEXT
    # ======================================

    context = build_product_scan_context(
        user_id
    )

    latest_face_scan = context.get(
        "latest_face_scan"
    )

    ingredient_memory = context.get(
        "ingredient_memory",
        {}
    )

    # ======================================
    # STEP 3
    # CONFLICT ENGINE
    # ======================================

    ingredient_conflicts = (
        analyze_ingredient_conflicts(

            current_ingredients=
                extracted.get(
                    "ingredients",
                    []
                ),

            ingredient_memory=
                ingredient_memory
        )
    )

    ingredient_intelligence = (
        calculate_ingredient_intelligence(

            current_ingredients=
                extracted.get(
                    "ingredients",
                    []
                ),

            ingredient_memory=
                ingredient_memory
        )
    )
        # ======================================
    # STEP 4
    # AI EXPLANATION
    # ======================================

    analysis_result = (
        await generate_product_analysis(

            extracted_product=extracted,

            face_scan=latest_face_scan,

            ingredient_memory=
                ingredient_memory,

            ingredient_conflicts=
                ingredient_conflicts,

            ingredient_intelligence=
                ingredient_intelligence
        )
    )

    # ======================================
    # FINAL RESPONSE
    # ======================================

    return {

        "product": {

            "name":
                extracted.get(
                    "product_name"
                ),

            "brand":
                extracted.get(
                    "brand"
                ),

            "category":
                extracted.get(
                    "category"
                )
        },

        "detected_ingredients":
            extracted.get(
                "ingredients",
                []
            ),

        "ingredient_conflicts":
            ingredient_conflicts,

        "ingredient_intelligence":
            ingredient_intelligence,

        **analysis_result
    }