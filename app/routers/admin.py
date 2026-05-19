from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase
from typing import Optional, List

router = APIRouter(prefix="/admin", tags=["Admin Upload"])


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


# ── NUTRITION CRUD (GET, PUT, DELETE) ──────────────────────────────────────────

@router.get("/nutrition")
async def list_nutrition():
    res = supabase.table("nutritions").select("*").order("priority", desc=True).execute()
    return res.data


@router.get("/nutrition/{id}")
async def get_nutrition(id: str):
    res = supabase.table("nutritions").select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(404, "Nutrition not found")
    return res.data[0]


@router.put("/nutrition/{id}")
async def update_nutrition(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None, examples=[""]),
    benefit: Optional[str] = Form(None, examples=[""]),
    tags: Optional[str] = Form(None, examples=[""]),
    priority: Optional[int] = Form(None)
):
    existing = supabase.table("nutritions").select("*").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Nutrition not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if benefit is not None:
        updates["benefit"] = benefit
    if tags is not None:
        updates["tags"] = clean_tags(tags)
    if priority is not None:
        updates["priority"] = priority
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"nutrition/{id}.png"
        await upload_image(file, file_path)
        updates["icon_url"] = get_public_url("assets", file_path)
        
    if updates:
        supabase.table("nutritions").update(updates).eq("id", id).execute()
        
    res = supabase.table("nutritions").select("*").eq("id", id).execute()
    return {"message": "Nutrition updated", "data": res.data[0]}


@router.delete("/nutrition/{id}")
async def delete_nutrition(id: str):
    existing = supabase.table("nutritions").select("id").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Nutrition not found")
    supabase.table("nutritions").delete().eq("id", id).execute()
    return {"message": "Nutrition deleted successfully"}


# ── FOOD CRUD (GET, PUT, DELETE) ──────────────────────────────────────────────

@router.get("/food")
async def list_food():
    res = supabase.table("foods").select("*").execute()
    return res.data


@router.get("/food/{id}")
async def get_food(id: str):
    res = supabase.table("foods").select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(404, "Food not found")
    return res.data[0]


@router.put("/food/{id}")
async def update_food(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None, examples=[""]),
    tags: Optional[str] = Form(None, examples=[""])
):
    existing = supabase.table("foods").select("*").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Food not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if tags is not None:
        updates["tags"] = clean_tags(tags)
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"food/{id}.png"
        await upload_image(file, file_path)
        updates["icon_url"] = get_public_url("assets", file_path)
        
    if updates:
        supabase.table("foods").update(updates).eq("id", id).execute()
        
    res = supabase.table("foods").select("*").eq("id", id).execute()
    return {"message": "Food updated", "data": res.data[0]}


@router.delete("/food/{id}")
async def delete_food(id: str):
    existing = supabase.table("foods").select("id").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Food not found")
    supabase.table("foods").delete().eq("id", id).execute()
    return {"message": "Food deleted successfully"}


# ── RECIPE CRUD (GET, PUT, DELETE) ────────────────────────────────────────────

@router.get("/recipe")
async def list_recipe():
    res = supabase.table("recipes").select("*").execute()
    return res.data


@router.get("/recipe/{id}")
async def get_recipe(id: str):
    res = supabase.table("recipes").select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(404, "Recipe not found")
    return res.data[0]


@router.put("/recipe/{id}")
async def update_recipe(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None, examples=[""]),
    description: Optional[str] = Form(None, examples=[""]),
    meal_type: Optional[str] = Form(None, examples=[""]),
    tags: Optional[str] = Form(None, examples=[""])
):
    existing = supabase.table("recipes").select("*").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Recipe not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if meal_type is not None:
        updates["meal_type"] = meal_type
    if tags is not None:
        updates["tags"] = clean_tags(tags)
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"recipe/{id}.png"
        await upload_image(file, file_path)
        updates["image_url"] = get_public_url("assets", file_path)
        
    if updates:
        supabase.table("recipes").update(updates).eq("id", id).execute()
        
    res = supabase.table("recipes").select("*").eq("id", id).execute()
    return {"message": "Recipe updated", "data": res.data[0]}


@router.delete("/recipe/{id}")
async def delete_recipe(id: str):
    existing = supabase.table("recipes").select("id").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Recipe not found")
    supabase.table("recipes").delete().eq("id", id).execute()
    return {"message": "Recipe deleted successfully"}