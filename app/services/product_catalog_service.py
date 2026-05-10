import uuid

from app.core.supabase_client import (
    supabase
)


# ==========================================
# FIND PRODUCT BY NAME
# ==========================================

def find_product_by_name(
    name: str
):

    response = supabase.table(
        "products"
    ).select("*").ilike(
        "name",
        name
    ).limit(1).execute()

    if response.data:

        return response.data[0]

    return None


# ==========================================
# CREATE PRODUCT
# ==========================================

def create_product_if_missing(

    extracted_product: dict,

    image_url: str = None
):

    existing = find_product_by_name(

        extracted_product.get(
            "product_name"
        )
    )

    if existing:

        return existing

    product_id = str(
        uuid.uuid4()
    )

    payload = {

        "id": product_id,

        "name":
            extracted_product.get(
                "product_name"
            ),

        "image_url":
            image_url,

        "category":
            extracted_product.get(
                "category"
            ),

        "tags":
            extracted_product.get(
                "ingredients",
                []
            ),

        "concerns": [],

        "priority": 99
    }

    response = supabase.table(
        "products"
    ).insert(
        payload
    ).execute()

    return response.data[0]