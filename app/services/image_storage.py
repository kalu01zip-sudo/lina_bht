import uuid
from app.core.s3_client import upload_file_to_s3
from app.utils.image_utils import optimise_image

async def upload_scan_image(file_bytes: bytes, user_id: str):
    try:
        # Optimise before upload
        file_bytes, _ = optimise_image(file_bytes, "image/jpeg")

        file_name = f"scan/{user_id}/{uuid.uuid4()}.jpg"

        # Upload to S3
        url = await upload_file_to_s3(file_bytes, file_name, "image/jpeg")
        return url

    except Exception as e:
        print("[ERROR] IMAGE UPLOAD ERROR:", e)
        return None