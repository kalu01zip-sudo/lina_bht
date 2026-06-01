import logging
from app.core.mongo_client import products_collection


def match_product(category: str, focus: str):
    category = category.lower().replace(" ", "_")
    focus = focus.lower().replace(" ", "_")

    # Try to find a product matching both category and concern
    doc = products_collection.find_one(
        {"category": category, "concerns": focus},
        {"_id": 0},
        sort=[("priority", 1)]
    )

    if doc:
        return doc

    # fallback — any product in category
    doc = products_collection.find_one(
        {"category": category},
        {"_id": 0}
    )
    return doc


def get_all_categories():
    try:
        categories = products_collection.distinct("category")
        return list(set(c for c in categories if c))
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to fetch product categories from MongoDB: %s", exc
        )
        return ["cleanser", "serum", "moisturizer", "sunscreen", "mask"]


def normalize_category(cat: str):
    mapping = {
        "gel cleanser": "cleanser",
        "foam cleanser": "cleanser",
        "face wash": "cleanser",

        "vitamin c serum": "serum",
        "brightening serum": "serum",

        "oil free moisturizer": "moisturizer",
        "hydrating cream": "moisturizer",

        "spf sunscreen": "sunscreen",
        "broad spectrum sunscreen": "sunscreen"
    }

    return mapping.get(cat.lower(), cat.lower())