from app.core.supabase_client import (
    supabase
)


# ==========================================
# CHECK DUPLICATE PRODUCT
# ==========================================

def check_duplicate_product(

    user_id: str,

    product_name: str
):

    response = supabase.table(
        "saved_routines"
    ).select("*").eq(
        "user_id",
        user_id
    ).eq(
        "product_name",
        product_name
    ).execute()

    return len(response.data) > 0