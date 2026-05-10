from app.core.supabase_client import (
    supabase
)


# ==========================================
# PRODUCT SCORING ENGINE
# ==========================================

def score_product(

    product: dict,

    user_concerns: list,

    allergies: list
):

    score = 0

    tags = product.get(
        "tags",
        []
    ) or []

    concerns = product.get(
        "concerns",
        []
    ) or []

    # ======================================
    # CONCERN MATCH
    # ======================================

    for concern in concerns:

        if concern in user_concerns:

            score += 30

    # ======================================
    # TAG MATCH
    # ======================================

    for tag in tags:

        if tag in user_concerns:

            score += 10

    # ======================================
    # ALLERGY PENALTY
    # ======================================

    for allergy in allergies:

        if allergy.lower() in str(tags).lower():

            score -= 100

    # ======================================
    # PRIORITY BONUS
    # ======================================

    priority = product.get(
        "priority",
        99
    )

    score += max(
        0,
        20 - priority
    )

    return score


# ==========================================
# FETCH BEST PRODUCT
# ==========================================

def fetch_best_product(

    category: str,

    user_concerns: list,

    allergies: list
):

    response = supabase.table(
        "products"
    ).select("*").eq(
        "category",
        category
    ).execute()

    products = response.data

    if not products:

        return None

    ranked = []

    for product in products:

        score = score_product(

            product=product,

            user_concerns=
                user_concerns,

            allergies=
                allergies
        )

        ranked.append({

            "score": score,

            "product": product
        })

    ranked.sort(

        key=lambda x: x["score"],

        reverse=True
    )

    return ranked[0]["product"]