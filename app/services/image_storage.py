import uuid
from app.core.supabase_client import supabase


async def upload_scan_image(file_bytes: bytes, user_id: str):
    try:
        file_name = f"scan/{user_id}/{uuid.uuid4()}.jpg"

        supabase.storage.from_("assets").upload(
            file_name,
            file_bytes,
            file_options={"content-type": "image/jpeg"}
        )

        url = supabase.storage.from_("assets").get_public_url(file_name)

        if isinstance(url, dict):
            return url.get("publicUrl")

        return url

    except Exception as e:
        print("❌ IMAGE UPLOAD ERROR:", e)
        return None