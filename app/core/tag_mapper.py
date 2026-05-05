# app/core/tag_mapper.py

SEMANTIC_TO_NUTRITION = {
    # general
    "healthy": ["vitamin_c", "vitamin_e"],
    "detox": ["vitamin_c", "antioxidant"],
    "antioxidant": ["vitamin_c", "vitamin_e"],

    # protein related
    "protein": ["zinc", "iron"],

    # fruit / glow
    "fruit": ["vitamin_c"],
    "glow": ["vitamin_c", "vitamin_e"],

    # hydration
    "hydration": ["omega_3", "electrolytes"],

    # skin repair
    "repair": ["vitamin_a", "zinc"],

    # energy / blood
    "iron_rich": ["iron"],

    # omega variations
    "omega": ["omega_3"],
    "omega 3": ["omega_3"],
}


def normalize_tag(tag: str) -> str:
    return tag.strip().lower().replace(" ", "_")


def expand_tags(tags: list[str]) -> list[str]:
    expanded = set()

    for tag in tags:
        t = normalize_tag(tag)

        if t in SEMANTIC_TO_NUTRITION:
            expanded.update(SEMANTIC_TO_NUTRITION[t])
        else:
            expanded.add(t)

    return list(expanded)