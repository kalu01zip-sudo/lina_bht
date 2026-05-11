from app.services.extract_product_ai import (
    extract_product_data,
    validate_cosmetic_product
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
    # STEP 0
    # COSMETIC VALIDATION (Token-saving check)
    # ======================================

    validation = await validate_cosmetic_product(
        image_bytes
    )

    if not validation.get("is_cosmetic"):
        # Return early with simple error message
        return {
            "product": {
                "name": "Not a skincare product",
                "brand": "Unknown",
                "category": "Non-cosmetic",
                "id": "not_a_skincare_product",
            },
            "detected_ingredients": [],
            "ingredient_conflicts": [],
            "ingredient_intelligence": {
                "irritation_load": 0,
                "exfoliation_load": 0,
                "barrier_stress": 0,
                "active_intensity": 0
            },
            "analysis": {
                "overall_score": 0,
                "score_profile": {
                    "compatibility": 0,
                    "safety": 0,
                    "redness": 0,
                    "effectiveness": 0,
                    "evenness": 0
                },
                "compatibility_analysis": {
                    "ingredient_conflict": {
                        "score": 0,
                        "intensity": "low",
                        "why": "This is not a skincare product"
                    },
                    "allergy_risk": {
                        "score": 0,
                        "intensity": "low",
                        "why": "Non-cosmetic items cannot be analyzed for skin compatibility"
                    }
                },
                "product_benefits": {
                    "high_compatibility": {
                        "score": 0,
                        "intensity": "low",
                        "why": "This item is not a topical skincare product"
                    },
                    "ingredient_synergy": {
                        "score": 0,
                        "intensity": "low",
                        "why": "No active skincare ingredients available"
                    }
                },
                "what_to_stop": [
                    "Scanning non-cosmetic items for skin analysis"
                ],
                "what_to_do": [
                    "Scan a verified topical skincare product instead"
                ],
                "learn_more": "Please scan a cosmetic or skincare product (cleanser, moisturizer, serum, sunscreen, etc.) for compatibility analysis."
            },
            "catalog_product": {
                "id": "not_a_skincare_product",
                "name": "Not a skincare product",
                "category": "non-cosmetic",
                "tags": [],
                "concerns": [],
                "priority": 99
            }
        }

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