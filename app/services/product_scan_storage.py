from app.core.s3_client import _upload_file_sync
import uuid

def upload_product_scan_image(
    image_bytes: bytes,
    content_type: str = "image/jpeg"
):
    filename = f"{uuid.uuid4()}.jpg"
    # Use path prefix to match old storage bucket name structure
    path = f"product-scans/scans/{filename}"

    # UPLOAD & GET URL
    public_url = _upload_file_sync(image_bytes, path, content_type)
    return public_url