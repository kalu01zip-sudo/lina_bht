"""
SkinSense — Mock Responses
Returned by all endpoints when MOCK_MODE=true in .env.
Useful for frontend development without burning API credits.
"""

from __future__ import annotations


# ═══════════════════════════════════════════════════════════════
#  /face_scan
# ═══════════════════════════════════════════════════════════════

FACE_SCAN: dict = {
    "success":   True,
    "mock":      True,
    "scan_type": "face",
    "score":     74,
    "score_recommendation_note": (
        "Mild acne and oiliness detected; start a gentle salicylic acid routine."
    ),
    "detected_conditions": [
        {
            "condition": "Mild Acne",
            "detail":    "Excess sebum clogging pores; try salicylic acid cleanser.",
            "seriousness": "mild"
        },
        {
            "condition": "Oily T-Zone",
            "detail":    "Overactive sebaceous glands; use a mattifying moisturiser daily.",
            "seriousness": "mild"
        },
        {
            "condition": "Early Pigmentation",
            "detail":    "Sun exposure causing dark spots; apply SPF 50+ sunscreen.",
            "seriousness": "mild"
        },
        {
            "condition": "Enlarged Pores",
            "detail":    "Use niacinamide serum to tighten and refine pores.",
            "seriousness": "mild"
        },
    ],
}


# ═══════════════════════════════════════════════════════════════
#  /hair_and_scalp_scan
# ═══════════════════════════════════════════════════════════════

HAIR_SCALP_SCAN: dict = {
    "success":   True,
    "mock":      True,
    "scan_type": "hair_and_scalp",
    "score":     61,
    "score_recommendation_note": (
        "Moderate dandruff and oily scalp detected; start an antifungal shampoo routine."
    ),
    "detected_conditions": [
        {
            "condition": "Moderate Dandruff",
            "detail":    "Malassezia overgrowth; use ketoconazole shampoo twice weekly.",
            "seriousness": "moderate"
        },
        {
            "condition": "Oily Scalp",
            "detail":    "Excess sebum production; wash hair every other day.",
            "seriousness": "mild"

        },
        {
            "condition": "Mild Hair Thinning",
            "detail":    "Early telogen effluvium; increase protein intake and reduce stress.",
            "seriousness": "mild"
        },
        {
            "condition": "Scalp Irritation",
            "detail":    "Possible product buildup; clarify with a gentle chelating shampoo.",
            "seriousness": "mild"
        },
    ],
}


# ═══════════════════════════════════════════════════════════════
#  /profile/score
# ═══════════════════════════════════════════════════════════════

PROFILE_SCORE: dict = {
    "success":     True,
    "mock":        True,
    "score":       78,
    "hydration":   62,
    "acne_risk":   "Mild",
    "sensitivity": "High",
    "note":        "Use gentle, fragrance-free products safe for pregnancy now.",
}


# ═══════════════════════════════════════════════════════════════
#  /product/scan
# ═══════════════════════════════════════════════════════════════

PRODUCT_SCAN: dict = {
    "success": True,
    "mock":    True,
    "barcode": "3600523457441",
    "product": {
        "name":              "Neutrogena Hydro Boost Water Gel",
        "brand":             "Neutrogena",
        "category":          "Moisturiser",
        "image_url":         "https://images.openbeautyfacts.org/mock/neutrogena-hydro-boost.jpg",
        "total_ingredients": 24,
    },
    "analysis": {
        "compatibility":       "Good",
        "compatibility_score": 82,
        "summary":             "This product suits oily skin well — 3 beneficial ingredient(s) found.",
        "good_ingredients": [
            {
                "ingredient":        "hyaluronic acid",
                "benefit":           "Powerful humectant — attracts and retains moisture",
                "comedogenic_rating": 0,
            },
            {
                "ingredient":        "glycerin",
                "benefit":           "Gentle humectant — draws water into skin",
                "comedogenic_rating": 0,
            },
            {
                "ingredient":        "niacinamide",
                "benefit":           "Reduces pore size and balances sebum production",
                "comedogenic_rating": 0,
            },
        ],
        "bad_ingredients":     [],
        "warnings":            [],
        "ingredients_checked": 30,
        "skin_type_analysed":  "oily",
    },
}