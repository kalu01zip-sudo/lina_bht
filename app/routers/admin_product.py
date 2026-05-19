from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase
from typing import Optional

router = APIRouter(prefix="/admin", tags=["Admin Upload"])


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
            file_options={
                "content-type": "image/jpeg",
                "x-upsert": "true"
            }
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


# ── PRODUCT CRUD (GET, PUT, DELETE) ───────────────────────────────────────────

@router.get("/product")
async def list_products():
    res = supabase.table("products").select("*").order("priority", desc=True).execute()
    return res.data


@router.get("/product/{id}")
async def get_product(id: str):
    res = supabase.table("products").select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(404, "Product not found")
    return res.data[0]


@router.put("/product/{id}")
async def update_product(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None, examples=[""]),
    category: Optional[str] = Form(None, examples=[""]),
    tags: Optional[str] = Form(None, examples=[""]),
    concerns: Optional[str] = Form(None, examples=[""]),
    priority: Optional[int] = Form(None)
):
    existing = supabase.table("products").select("*").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Product not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if category is not None:
        updates["category"] = category.lower()
    if tags is not None:
        updates["tags"] = clean_list(tags)
    if concerns is not None:
        updates["concerns"] = clean_list(concerns)
    if priority is not None:
        updates["priority"] = priority
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"products/{id}.jpg"
        await upload_image(file, file_path)
        updates["image_url"] = get_public_url("assets", file_path)
        
    if updates:
        supabase.table("products").update(updates).eq("id", id).execute()
        
    res = supabase.table("products").select("*").eq("id", id).execute()
    return {"message": "Product updated successfully", "data": res.data[0]}


@router.delete("/product/{id}")
async def delete_product(id: str):
    existing = supabase.table("products").select("id").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Product not found")
    supabase.table("products").delete().eq("id", id).execute()
    return {"message": "Product deleted successfully"}
    