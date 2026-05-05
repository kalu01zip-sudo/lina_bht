import uuid
from app.services.product_matcher import match_product


def build_routine(ai_data):

    def enrich(step):
        category = step.get("product_category")
        focus = step.get("focus")
        phase = step.get("phase", "maintenance")

        product = match_product(category, focus) if category else None

        return {
            "id": str(uuid.uuid4()),
            "phase": phase,
            "product_category": category,
            "product_name": product.get("name") if product else None,
            "product_url": product.get("image_url") if product else None,
            "why": f"Targets {focus} and supports skin balance effectively."
        }

    return {
        "why": ai_data.get("why", []),
        "morning": [enrich(s) for s in ai_data.get("morning", [])],
        "night": [enrich(s) for s in ai_data.get("night", [])],
        "weekly_care": [enrich(s) for s in ai_data.get("weekly_care", [])]
    }