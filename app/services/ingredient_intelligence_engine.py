from app.core.ingredient_profiles import (
    INGREDIENT_PROFILES
)


# ==========================================
# SAFE NORMALIZER
# ==========================================

def normalize(value):

    return min(
        int(value * 10),
        100
    )


# ==========================================
# INGREDIENT INTELLIGENCE ENGINE
# ==========================================

def calculate_ingredient_intelligence(

    current_ingredients: list,

    ingredient_memory: dict
):

    all_ingredients = []

    # ======================================
    # CURRENT INGREDIENTS
    # ======================================

    for item in current_ingredients:

        all_ingredients.append(
            item.lower()
        )

    # ======================================
    # MEMORY INGREDIENTS
    # ======================================

    for ingredient, count in ingredient_memory.get(

        "ingredient_frequency",
        {}

    ).items():

        for _ in range(count):

            all_ingredients.append(
                ingredient.lower()
            )

    # ======================================
    # SCORE ACCUMULATION
    # ======================================

    irritation = 0

    exfoliation = 0

    barrier = 0

    intensity = 0

    counted = 0

    for ingredient in all_ingredients:

        profile = INGREDIENT_PROFILES.get(
            ingredient
        )

        if not profile:
            continue

        irritation += profile[
            "irritation"
        ]

        exfoliation += profile[
            "exfoliation"
        ]

        barrier += profile[
            "barrier_stress"
        ]

        intensity += profile[
            "intensity"
        ]

        counted += 1

    if counted == 0:

        return {

            "irritation_load": 0,

            "exfoliation_load": 0,

            "barrier_stress": 0,

            "active_intensity": 0
        }

    # ======================================
    # NORMALIZED SCORES
    # ======================================

    return {

        "irritation_load":
            normalize(
                irritation / counted
            ),

        "exfoliation_load":
            normalize(
                exfoliation / counted
            ),

        "barrier_stress":
            normalize(
                barrier / counted
            ),

        "active_intensity":
            normalize(
                intensity / counted
            )
    }