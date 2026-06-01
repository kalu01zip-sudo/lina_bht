from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional, List
from app.core.s3_client import upload_file_to_s3
from app.core.mongo_client import nutritions_collection, foods_collection, recipes_collection

router = APIRouter(prefix="/admin", tags=["Admin Upload"])


# =========================
# COMMON HELPERS
# =========================

def normalize_id(raw_id: str) -> str:
    return raw_id.strip().lower().replace(" ", "_")


def clean_list(val: str) -> List[str]:
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def clean_conditions(val: str) -> List[str]:
    if not val:
        return []
    # Normalize condition names to lower case and snake case
    return [x.strip().lower().replace(" ", "_") for x in val.split(",") if x.strip()]


async def upload_image(file: UploadFile, file_path: str) -> str:
    file_bytes = await file.read()
    try:
        url = await upload_file_to_s3(
            file_bytes,
            file_path,
            file.content_type or "image/png"
        )
        return url
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
    main_ingredient: str = Form(...),
    detected_condition: str = Form(...),
    how_it_improves: str = Form(...),
    links: str = Form(""),
    priority: int = Form(1)
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        cond_list = clean_conditions(detected_condition)
        link_list = clean_list(links)

        # check duplicate
        existing = nutritions_collection.find_one({"id": id})
        if existing:
            raise HTTPException(400, "ID already exists")

        file_path = f"nutrition/{id}.png"
        public_url = await upload_image(file, file_path)

        nutritions_collection.insert_one({
            "id": id,
            "name": name,
            "main_ingredient": main_ingredient,
            "detected_condition": cond_list,
            "how_it_improves": how_it_improves,
            "links": link_list,
            "icon_url": public_url,
            "priority": priority,
            # Backwards compatibility
            "benefit": how_it_improves,
            "tags": cond_list
        })

        return {"message": "Nutrition uploaded", "url": public_url}

    except HTTPException:
        raise
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
    ingredients: str = Form(...),
    detected_condition: str = Form(...),
    benefits: str = Form(...),
    links: str = Form("")
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        ing_list = clean_list(ingredients)
        cond_list = clean_conditions(detected_condition)
        link_list = clean_list(links)

        existing = foods_collection.find_one({"id": id})
        if existing:
            raise HTTPException(400, "ID already exists")

        file_path = f"food/{id}.png"
        public_url = await upload_image(file, file_path)

        foods_collection.insert_one({
            "id": id,
            "name": name,
            "ingredients": ing_list,
            "detected_condition": cond_list,
            "benefits": benefits,
            "links": link_list,
            "icon_url": public_url,
            # Backwards compatibility
            "tags": cond_list
        })

        return {"message": "Food uploaded", "url": public_url}

    except HTTPException:
        raise
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
    recipe_name: str = Form(...),
    main_ingredients: str = Form(...),
    detected_condition: str = Form(...),
    how_it_improves: str = Form(...),
    tags: str = Form(""),
    links: str = Form("")
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        id = normalize_id(id)
        ing_list = clean_list(main_ingredients)
        cond_list = clean_conditions(detected_condition)
        tag_list = clean_list(tags)
        link_list = clean_list(links)

        existing = recipes_collection.find_one({"id": id})
        if existing:
            raise HTTPException(400, "ID already exists")

        file_path = f"recipe/{id}.png"
        public_url = await upload_image(file, file_path)

        recipes_collection.insert_one({
            "id": id,
            "recipe_name": recipe_name,
            "main_ingredients": ing_list,
            "detected_condition": cond_list,
            "how_it_improves": how_it_improves,
            "tags": tag_list,
            "links": link_list,
            "image_url": public_url,
            # Backwards compatibility
            "name": recipe_name,
            "description": how_it_improves,
            "meal_type": "main"
        })

        return {"message": "Recipe uploaded", "url": public_url}

    except HTTPException:
        raise
    except Exception as e:
        print("ERROR:", e)
        raise HTTPException(500, str(e))


# ── NUTRITION CRUD (GET, PUT, DELETE) ──────────────────────────────────────────

@router.get("/nutrition")
async def list_nutrition():
    cursor = nutritions_collection.find({}).sort("priority", -1)
    results = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


@router.get("/nutrition/{id}")
async def get_nutrition(id: str):
    doc = nutritions_collection.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Nutrition not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/nutrition/{id}")
async def update_nutrition(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None),
    main_ingredient: Optional[str] = Form(None),
    detected_condition: Optional[str] = Form(None),
    how_it_improves: Optional[str] = Form(None),
    links: Optional[str] = Form(None),
    priority: Optional[int] = Form(None)
):
    existing = nutritions_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Nutrition not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if main_ingredient is not None:
        updates["main_ingredient"] = main_ingredient
    if detected_condition is not None:
        cond_list = clean_conditions(detected_condition)
        updates["detected_condition"] = cond_list
        updates["tags"] = cond_list
    if how_it_improves is not None:
        updates["how_it_improves"] = how_it_improves
        updates["benefit"] = how_it_improves
    if links is not None:
        updates["links"] = clean_list(links)
    if priority is not None:
        updates["priority"] = priority
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"nutrition/{id}.png"
        public_url = await upload_image(file, file_path)
        updates["icon_url"] = public_url
        
    if updates:
        nutritions_collection.update_one({"id": id}, {"$set": updates})
        
    res = nutritions_collection.find_one({"id": id})
    res["_id"] = str(res["_id"])
    return {"message": "Nutrition updated", "data": res}


@router.delete("/nutrition/{id}")
async def delete_nutrition(id: str):
    existing = nutritions_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Nutrition not found")
    nutritions_collection.delete_one({"id": id})
    return {"message": "Nutrition deleted successfully"}


# ── FOOD CRUD (GET, PUT, DELETE) ──────────────────────────────────────────────

@router.get("/food")
async def list_food():
    cursor = foods_collection.find({})
    results = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


@router.get("/food/{id}")
async def get_food(id: str):
    doc = foods_collection.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Food not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/food/{id}")
async def update_food(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None),
    ingredients: Optional[str] = Form(None),
    detected_condition: Optional[str] = Form(None),
    benefits: Optional[str] = Form(None),
    links: Optional[str] = Form(None)
):
    existing = foods_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Food not found")
        
    updates = {}
    if name is not None:
        updates["name"] = name
    if ingredients is not None:
        updates["ingredients"] = clean_list(ingredients)
    if detected_condition is not None:
        cond_list = clean_conditions(detected_condition)
        updates["detected_condition"] = cond_list
        updates["tags"] = cond_list
    if benefits is not None:
        updates["benefits"] = benefits
    if links is not None:
        updates["links"] = clean_list(links)
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"food/{id}.png"
        public_url = await upload_image(file, file_path)
        updates["icon_url"] = public_url
        
    if updates:
        foods_collection.update_one({"id": id}, {"$set": updates})
        
    res = foods_collection.find_one({"id": id})
    res["_id"] = str(res["_id"])
    return {"message": "Food updated", "data": res}


@router.delete("/food/{id}")
async def delete_food(id: str):
    existing = foods_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Food not found")
    foods_collection.delete_one({"id": id})
    return {"message": "Food deleted successfully"}


# ── RECIPE CRUD (GET, PUT, DELETE) ────────────────────────────────────────────

@router.get("/recipe")
async def list_recipe():
    cursor = recipes_collection.find({})
    results = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


@router.get("/recipe/{id}")
async def get_recipe(id: str):
    doc = recipes_collection.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Recipe not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/recipe/{id}")
async def update_recipe(
    id: str,
    file: UploadFile = File(None),
    recipe_name: Optional[str] = Form(None),
    main_ingredients: Optional[str] = Form(None),
    detected_condition: Optional[str] = Form(None),
    how_it_improves: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    links: Optional[str] = Form(None)
):
    existing = recipes_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Recipe not found")
        
    updates = {}
    if recipe_name is not None:
        updates["recipe_name"] = recipe_name
        updates["name"] = recipe_name
    if main_ingredients is not None:
        updates["main_ingredients"] = clean_list(main_ingredients)
    if detected_condition is not None:
        updates["detected_condition"] = clean_conditions(detected_condition)
    if how_it_improves is not None:
        updates["how_it_improves"] = how_it_improves
        updates["description"] = how_it_improves
    if tags is not None:
        updates["tags"] = clean_list(tags)
    if links is not None:
        updates["links"] = clean_list(links)
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"recipe/{id}.png"
        public_url = await upload_image(file, file_path)
        updates["image_url"] = public_url
        
    if updates:
        recipes_collection.update_one({"id": id}, {"$set": updates})
        
    res = recipes_collection.find_one({"id": id})
    res["_id"] = str(res["_id"])
    return {"message": "Recipe updated", "data": res}


@router.delete("/recipe/{id}")
async def delete_recipe(id: str):
    existing = recipes_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Recipe not found")
    recipes_collection.delete_one({"id": id})
    return {"message": "Recipe deleted successfully"}