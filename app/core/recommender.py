# app/core/recommender.py

def score_item(item_tags: list[str], nutrition_ids: list[str]) -> int:
    score = 0

    for tag in item_tags:
        if tag in nutrition_ids:
            score += 10

    return score


def rank_items(items: list[dict], nutrition_ids: list[str]):
    scored = []

    for item in items:
        tags = item.get("tags", [])
        s = score_item(tags, nutrition_ids)

        if s > 0:
            item["score"] = s
            scored.append(item)

    # sort high → low
    return sorted(scored, key=lambda x: x["score"], reverse=True)

def boost_by_context(item, ai_data):
    from app.core.mapping import normalize_condition

    boost = 0

    # 1. Normalize item conditions and tags
    item_conditions = item.get("detected_condition", [])
    if isinstance(item_conditions, str):
        item_conditions = [item_conditions]
    elif not isinstance(item_conditions, list):
        item_conditions = []
    
    normalized_item_conditions = {normalize_condition(c) for c in item_conditions if isinstance(c, str)}
    
    item_tags = item.get("tags", [])
    if isinstance(item_tags, str):
        item_tags = [item_tags]
    elif not isinstance(item_tags, list):
        item_tags = []
        
    normalized_item_tags = {normalize_condition(t) for t in item_tags if isinstance(t, str)}

    # 2. Hydration boost (skin scans)
    hydration = ai_data.get("hydration")
    if hydration is not None:
        try:
            hydration_val = int(hydration)
        except (TypeError, ValueError):
            hydration_val = 50
        if hydration_val < 50 and "hydration" in normalized_item_tags:
            boost += 5

    # 3. Scalp health boost (scalp scans)
    scalp_health = ai_data.get("scalp_health")
    if scalp_health is not None:
        try:
            sh_val = int(scalp_health)
        except (TypeError, ValueError):
            sh_val = 100
        if sh_val < 50:
            scalp_conditions = {
                "dandruff", "oily_scalp", "dry_scalp", "product_buildup", "hair_thinning",
                "hair_loss", "split_ends", "brittle_hair", "scalp_acne", "inflammation",
                "seborrheic_dermatitis", "alopecia", "telogen_effluvium", "androgenetic_alopecia",
                "scalp_fungus", "scalp_irritation", "hair_breakage", "sebum_overproduction",
                "hair_color_damage", "heat_damage", "chemical_damage", "hair", "scalp"
            }
            if any(sc in normalized_item_conditions or sc in normalized_item_tags for sc in scalp_conditions):
                boost += 5

    # 4. Targeted severity boost (only if the item targets the specific condition)
    for cond in ai_data.get("detected_condition", []):
        if not isinstance(cond, dict):
            continue
        cname = normalize_condition(cond.get("name", ""))
        if cname in normalized_item_conditions or cname in normalized_item_tags:
            severity = cond.get("severity", "Moderate")
            if severity == "Severe":
                boost += 5
            elif severity == "Moderate":
                boost += 3

    return boost


def smart_rank(items, nutrition_ids, ai_data):
    scored = []

    for item in items:
        base = score_item(item.get("tags", []), nutrition_ids)
        boost = boost_by_context(item, ai_data)

        total = base + boost

        if total > 0:
            item["score"] = total
            scored.append(item)

    return sorted(scored, key=lambda x: x["score"], reverse=True)