from app.core.supabase_client import supabase

import uuid


# ==========================================
# UPLOAD PRODUCT SCAN IMAGE
# ==========================================

def upload_product_scan_image(

    image_bytes: bytes,

    content_type: str = "image/jpeg"
):

    filename = f"{uuid.uuid4()}.jpg"

    path = f"scans/{filename}"

    # ======================================
    # UPLOAD
    # ======================================

    supabase.storage \
        .from_("product-scans") \
        .upload(

            path,

            image_bytes,

            file_options={
                "content-type": content_type
            }
        )

    # ======================================
    # PUBLIC URL
    # ======================================

    public_url = supabase.storage \
        .from_("product-scans") \
        .get_public_url(path)

    return public_url