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
    boost = 0

    hydration = ai_data.get("hydration", 50)

    # low hydration → boost hydration foods
    if hydration < 50 and "hydration" in item.get("tags", []):
        boost += 5

    # severity boost
    for cond in ai_data.get("detected_condition", []):
        if cond["severity"] == "Severe":
            boost += 5
        elif cond["severity"] == "Moderate":
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