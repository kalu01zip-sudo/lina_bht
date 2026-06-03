import logging
from app.core.mongo_client import products_collection
from app.services.product_catalog_service import serialize_product_doc


def match_product(category: str, focus: str):
    category = category.lower().replace(" ", "_")
    focus = focus.lower().replace(" ", "_")

    # Try to find a product matching both category and concern
    doc = products_collection.find_one(
        {
            "$and": [
                {"$or": [{"categories": category}, {"category": category}]},
                {"$or": [{"detected_conditions": focus}, {"concerns": focus}]},
            ]
        },
        sort=[("priority", 1)]
    )

    if doc:
        return serialize_product_doc(doc)

    # fallback — any product in category
    doc = products_collection.find_one(
        {"$or": [{"categories": category}, {"category": category}]}
    )
    return serialize_product_doc(doc)


def get_all_categories():
    try:
        legacy_categories = products_collection.distinct("category")
        categories = products_collection.distinct("categories")
        return list(set(c for c in [*legacy_categories, *categories] if c))
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
