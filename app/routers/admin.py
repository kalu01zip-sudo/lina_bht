from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase

router = APIRouter(prefix="/admin", tags=["Admin"])


# =========================
# COMMON HELPERS
# =========================

def normalize_id(raw_id: str) -> str:
    return raw_id.strip().lower().replace(" ", "_")


def clean_tags(tags: str):
    return [t.strip().replace('"', '') for t in tags.split(",") if t]


def get_public_url(bucket: str, path: str):
    url = supabase.storage.from_(bucket).get_public_url(path)
    if isinstance(url, dict):
        return url.get("publicUrl")
    return url

async def upload_image(file: UploadFile, file_path: str):
    file_bytes = await file.read()

    try:
        res = supabase.storage.from_("assets").upload(
            file_path,
            file_bytes,
            {
                "content-type": file.content_type,
                "x-upsert": "true"
            }
        )
        return res
    except Exception as e:
        print("UPLOAD ERROR:", e)
        raise HTTPException(500, f"Upload failed: {str(e)}")

# =========================
# 1. NUTRITION API
# =========================

@router.post("/nutrition")
async def upload_nutrition(
    file: UploadFile = File(...),
    id: str = Form(...),
    name: str = Form(...),
    benefit: str = Form(...),
    tags: str = Form(""),
    priority: int = Form(1)
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        tags_list = clean_tags(tags)

        # check duplicate
        existing = supabase.table("nutritions").select("id").eq("id", id).execute()
        if existing.data:
            raise HTTPException(400, "ID already exists")

        file_path = f"nutrition/{id}.png"

        await upload_image(file, file_path)

        public_url = get_public_url("assets", file_path)

        supabase.table("nutritions").insert({
            "id": id,
            "name": name,
            "icon_url": public_url,
            "benefit": benefit,
            "tags": tags_list,
            "priority": priority
        }).execute()

        return {"message": "Nutrition uploaded", "url": public_url}

    except Exception as e:
        print("ERROR:", e)
        raise HTTPException(500, str(e))


# =========================
# 2. FOOD API
# =========================

@router.post("/food")
async def upload_food(
    file: UploadFile = File(...),
    id: str = Form(...),
    name: str = Form(...),
    tags: str = Form("")
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        tags_list = clean_tags(tags)

        existing = supabase.table("foods").select("id").eq("id", id).execute()
        if existing.data:
            raise HTTPException(400, "ID already exists")

        file_path = f"food/{id}.png"

        await upload_image(file, file_path)

        public_url = get_public_url("assets", file_path)

        supabase.table("foods").insert({
            "id": id,
            "name": name,
            "icon_url": public_url,
            "tags": tags_list
        }).execute()

        return {"message": "Food uploaded", "url": public_url}

    except Exception as e:
        print("ERROR:", e)
        raise HTTPException(500, str(e))


# =========================
# 3. RECIPE API
# =========================

@router.post("/recipe")
async def upload_recipe(
    file: UploadFile = File(...),
    id: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    meal_type: str = Form(...),
    tags: str = Form("")
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        tags_list = clean_tags(tags)

        existing = supabase.table("recipes").select("id").eq("id", id).execute()
        if existing.data:
            raise HTTPException(400, "ID already exists")

        file_path = f"recipe/{id}.png"

        await upload_image(file, file_path)

        public_url = get_public_url("assets", file_path)

        supabase.table("recipes").insert({
            "id": id,
            "name": name,
            "image_url": public_url,
            "description": description,
            "meal_type": meal_type,
            "tags": tags_list
        }).execute()

        return {"message": "Recipe uploaded", "url": public_url}

    except Exception as e:
        print("ERROR:", e)
        raise HTTPException(500, str(e))