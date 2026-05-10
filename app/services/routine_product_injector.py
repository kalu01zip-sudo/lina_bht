from app.services.product_ranker import (
    fetch_best_product
)

# ==========================================
# INJECT REAL PRODUCTS
# ==========================================

def inject_products_into_routine(

    routine_data: dict,
    user_context: dict
):

    steps = routine_data[
        "routine"
    ][
        "steps"
    ]

    enhanced_steps = []

    for step in steps:

        category = step.get(
            "category"
        )

        product = fetch_best_product(
            category=category,
            user_concerns=
                user_context.get(
                    "user_concerns",
                    []
                ),
            allergies=
                user_context.get(
                    "allergies",
                    []
                )
        )

        enhanced_steps.append({

            "step":
                step["step"],

            "phase":
                routine_data[
                    "routine"
                ][
                    "phase"
                ],

            "product_id":
                product.get("id")
                if product else None,

            "product_name":
                product.get("name")
                if product else None,

            "product_url":
                product.get("image_url")
                if product else None,

            "product_category":
                category,

            "usage_reason":
                step.get(
                    "usage_reason"
                )
        })

    routine_data[
        "routine"
    ][
        "steps"
    ] = enhanced_steps

    return routine_data