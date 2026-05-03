# app/core/mapping.py

# Normalize incoming condition names
def normalize_condition(name: str) -> str:
    return name.lower().strip()


# Alias mapping (AI → system standard)
CONDITION_ALIAS = {
    "seborrheic dermatitis": "acne",
    "hyperpigmentation": "pigmentation",
    "dehydration": "dryness",
    "redness irritation": "irritation",
    "skin dehydration": "dryness",
    "sebaceous hypersecretion": "acne",
}


def resolve_condition(name: str) -> str:
    name = normalize_condition(name)
    return CONDITION_ALIAS.get(name, name)


# Condition → Nutrition mapping
CONDITION_TO_NUTRITION = {
    "acne": ["zinc", "vitamin_a"],
    "blackheads": ["zinc"],
    "whiteheads": ["zinc"],
    "pigmentation": ["vitamin_c"],
    "dark_spots": ["vitamin_c"],
    "uneven_tone": ["vitamin_c"],
    "dullness": ["vitamin_c", "iron"],
    "dryness": ["omega_3"],
    "dehydration": ["electrolytes"],
    "oiliness": ["vitamin_b6"],
    "pores": ["niacin"],
    "redness": ["vitamin_e"],
    "irritation": ["omega_3"],
    "sensitivity": ["vitamin_e"],
    "dark_circles": ["iron"],
    "eye_bags": ["potassium"],
    "fine_lines": ["collagen", "vitamin_c"],
    "wrinkles": ["collagen"],
    "loss_of_elasticity": ["collagen"],
    "sun_damage": ["vitamin_c", "vitamin_e"]
}


def extract_nutrition(ai_data: dict) -> list[str]:
    nutrition_set = set()

    conditions = ai_data.get("detected_condition", [])

    for c in conditions:
        raw_name = c.get("name", "")
        key = resolve_condition(raw_name)

        if key in CONDITION_TO_NUTRITION:
            nutrition_set.update(CONDITION_TO_NUTRITION[key])

    return list(nutrition_set)