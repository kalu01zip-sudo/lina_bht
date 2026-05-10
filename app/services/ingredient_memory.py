from collections import Counter

from app.services.product_scan_history import (
    get_recent_product_scans
)


# ==========================================
# BUILD INGREDIENT MEMORY
# ==========================================

def build_ingredient_memory(
    user_id: str
):

    scans = get_recent_product_scans(
        user_id=user_id,
        months=2
    )

    ingredient_counter = Counter()

    ingredient_sources = {}

    # ======================================
    # COLLECT INGREDIENTS
    # ======================================

    for scan in scans:

        ingredients = scan.get(
            "detected_ingredients",
            []
        )

        product = scan.get(
            "product",
            {}
        )

        product_name = product.get(
            "name"
        )

        for ingredient in ingredients:

            normalized = ingredient.lower()

            ingredient_counter[
                normalized
            ] += 1

            if normalized not in ingredient_sources:

                ingredient_sources[
                    normalized
                ] = []

            ingredient_sources[
                normalized
            ].append(product_name)

    # ======================================
    # BUILD OVERUSED INGREDIENTS
    # ======================================

    overused = []

    for ingredient, count in ingredient_counter.items():

        if count >= 2:

            overused.append({

                "ingredient": ingredient,

                "count": count,

                "products":
                    ingredient_sources[
                        ingredient
                    ]
            })

    return {

        "ingredient_frequency":
            dict(ingredient_counter),

        "overused_ingredients":
            overused
    }