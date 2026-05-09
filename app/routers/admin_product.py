from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase

router = APIRouter(prefix="/admin", tags=["Admin"])


# =========================
# HELPERS
# =========================

def normalize_id(raw_id: str) -> str:
    return raw_id.strip().lower().replace(" ", "_")


def clean_list(data: str):
    return [x.strip().lower().replace(" ", "_") for x in data.split(",") if x]


def get_public_url(bucket: str, path: str):
    url = supabase.storage.from_(bucket).get_public_url(path)
    if isinstance(url, dict):
        return url.get("publicUrl")
    return url


from PIL import Image
import io

async def upload_image(file: UploadFile, path: str):
    try:
        contents = await file.read()

        # open image
        image = Image.open(io.BytesIO(contents))

        # convert to RGB (important for PNG/WebP issues)
        if image.mode != "RGB":
            image = image.convert("RGB")

        # 🔥 resize to 240px width
        base_width = 240
        w_percent = base_width / float(image.size[0])
        h_size = int((float(image.size[1]) * float(w_percent)))

        image = image.resize((base_width, h_size), Image.LANCZOS)

        # save to bytes
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)

        # upload to supabase
        supabase.storage.from_("assets").upload(
            path,
            buffer.read(),
            file_options={"content-type": "image/jpeg"}
        )

    except Exception as e:
        print("UPLOAD ERROR:", e)
        raise HTTPException(500, f"Image processing failed: {str(e)}")


# =========================
# PRODUCT API
# =========================

@router.post("/product")
async def upload_product(
    file: UploadFile = File(...),
    id: str = Form(...),
    name: str = Form(...),
    category: str = Form(...),
    tags: str = Form(...),
    concerns: str = Form(...),
    priority: int = Form(1)
):
    try:
        # 🔥 validate image
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        # normalize
        id = normalize_id(id)
        tags_list = clean_list(tags)
        concerns_list = clean_list(concerns)

        # check duplicate
        existing = supabase.table("products") \
            .select("id") \
            .eq("id", id) \
            .execute()

        if existing.data:
            raise HTTPException(400, "Product ID already exists")

        # upload image
        file_path = f"products/{id}.jpg"
        await upload_image(file, file_path)

        public_url = get_public_url("assets", file_path)

        # insert into DB
        supabase.table("products").insert({
            "id": id,
            "name": name,
            "image_url": public_url,
            "category": category.lower(),
            "tags": tags_list,          
            "concerns": concerns_list,  
            "priority": priority
        }).execute()

        return {
            "message": "Product uploaded successfully",
            "id": id,
            "image_url": public_url
        }

    except Exception as e:
        raise HTTPException(500, str(e))
    