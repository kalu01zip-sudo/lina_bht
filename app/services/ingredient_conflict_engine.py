from app.core.ingredient_conflicts import (
    INGREDIENT_CONFLICTS
)


# ==========================================
# ANALYZE INGREDIENT CONFLICTS
# ==========================================

def analyze_ingredient_conflicts(

    current_ingredients: list,

    ingredient_memory: dict
):

    memory_ingredients = []

    # ======================================
    # MEMORY INGREDIENTS
    # ======================================

    for item in ingredient_memory.get(
        "ingredient_frequency",
        {}
    ).keys():

        memory_ingredients.append(
            item.lower()
        )

    current_ingredients = [

        i.lower()
        for i in current_ingredients
    ]

    conflicts = []

    # ======================================
    # CHECK CONFLICTS
    # ======================================

    for conflict in INGREDIENT_CONFLICTS:

        ingredient_a = conflict[
            "ingredients"
        ][0]

        ingredient_b = conflict[
            "ingredients"
        ][1]

        # current + history
        condition_1 = (
            ingredient_a in current_ingredients
            and
            ingredient_b in memory_ingredients
        )

        condition_2 = (
            ingredient_b in current_ingredients
            and
            ingredient_a in memory_ingredients
        )

        if condition_1 or condition_2:

            conflicts.append({

                "ingredients":
                    conflict["ingredients"],

                "risk":
                    conflict["risk"],

                "reason":
                    conflict["reason"]
            })

    return conflicts