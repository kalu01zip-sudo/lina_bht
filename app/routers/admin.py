from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional, List
import uuid
import io
from PIL import Image
from bson import ObjectId
from app.core.s3_client import upload_file_to_s3
from app.core.mongo_client import nutritions_collection, foods_collection, recipes_collection

router = APIRouter(prefix="/admin", tags=["Admin Upload"])


# =========================
# COMMON HELPERS
# =========================

def normalize_id(raw_id: str) -> str:
    return raw_id.strip().lower().replace(" ", "_")


def get_nutrition_filter(id_str: str) -> dict:
    normalized = normalize_id(id_str)
    try:
        oid = ObjectId(id_str)
        return {"$or": [{"id": normalized}, {"_id": oid}]}
    except Exception:
        return {"id": normalized}


def get_food_filter(id_str: str) -> dict:
    normalized = normalize_id(id_str)
    try:
        oid = ObjectId(id_str)
        return {"$or": [{"id": normalized}, {"_id": oid}]}
    except Exception:
        return {"id": normalized}

def get_recipe_filter(id_str: str) -> dict:
    normalized = normalize_id(id_str)
    try:
        oid = ObjectId(id_str)
        return {"$or": [{"id": normalized}, {"_id": oid}]}
    except Exception:
        return {"id": normalized}


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
    name: str = Form(...),
    main_ingredient: str = Form(...),
    detected_condition: str = Form(...),
    how_it_improves: str = Form(...)
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        base_id = normalize_id(name)
        id = base_id
        while nutritions_collection.find_one({"id": id}):
            id = f"{base_id}_{uuid.uuid4().hex[:6]}"

        cond_list = clean_conditions(detected_condition)
        link_list = []
        priority = 1

        # Read, resize to 128x128, and convert to PNG bytes
        try:
            file_bytes = await file.read()
            img = Image.open(io.BytesIO(file_bytes))
            img = img.resize((64, 64), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            img.save(output, format="PNG")
            resized_bytes = output.getvalue()
        except Exception as e:
            raise HTTPException(400, f"Invalid image or resizing failed: {str(e)}")

        file_path = f"nutrition/{id}.png"
        try:
            public_url = await upload_file_to_s3(
                resized_bytes,
                file_path,
                "image/png"
            )
        except Exception as e:
            print("UPLOAD ERROR:", e)
            raise HTTPException(500, f"Upload failed: {str(e)}")

        nutritions_collection.insert_one({
            "id": id,
            "name": name,
            "main ingredient": main_ingredient,
            "detected conditions": cond_list,
            "how to improves": how_it_improves,
            "image url": public_url
        })

        return {"message": "Nutrition uploaded", "id": id, "url": public_url}

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
    name: str = Form(...),
    ingredients: str = Form(...),
    detected_condition: str = Form(...),
    benefits: str = Form(...)
):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        base_id = normalize_id(name)
        id = base_id
        while foods_collection.find_one({"id": id}):
            id = f"{base_id}_{uuid.uuid4().hex[:6]}"

        ing_list = clean_list(ingredients)
        cond_list = clean_conditions(detected_condition)

        # Read, resize to 128x128, and convert to PNG bytes
        try:
            file_bytes = await file.read()
            img = Image.open(io.BytesIO(file_bytes))
            img = img.resize((128, 128), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            img.save(output, format="PNG")
            resized_bytes = output.getvalue()
        except Exception as e:
            raise HTTPException(400, f"Invalid image or resizing failed: {str(e)}")

        file_path = f"food/{id}.png"
        try:
            public_url = await upload_file_to_s3(
                resized_bytes,
                file_path,
                "image/png"
            )
        except Exception as e:
            print("UPLOAD ERROR:", e)
            raise HTTPException(500, f"Upload failed: {str(e)}")

        foods_collection.insert_one({
            "id": id,
            "name": name,
            "ingredients": ing_list,
            "detected_condition": cond_list,
            "benefits": benefits,
            "icon_url": public_url,
            # Backwards compatibility
            "tags": cond_list
        })

        return {"message": "Food uploaded", "id": id, "url": public_url}

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
    name: str = Form(...),
    main_ingredients: str = Form(...),
    detected_condition: str = Form(...),
    how_it_improves: str = Form(...),
    tags: str = Form("")
):
    try:
        # Validate image type
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")

        # Generate ID from recipe name and ensure uniqueness
        base_id = normalize_id(name)
        id = base_id
        while recipes_collection.find_one({"id": id}):
            id = f"{base_id}_{uuid.uuid4().hex[:6]}"
        ing_list = clean_list(main_ingredients)
        cond_list = clean_conditions(detected_condition)
        tag_list = clean_list(tags)

        # Ensure no duplicate ID
        if recipes_collection.find_one({"id": id}):
            raise HTTPException(400, "ID already exists")

        # Resize image to 1920x1080 and upload to S3
        try:
            file_bytes = await file.read()
            img = Image.open(io.BytesIO(file_bytes))
            img = img.resize((1280, 720), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            img.save(output, format="PNG")
            resized_bytes = output.getvalue()
        except Exception as e:
            raise HTTPException(400, f"Invalid image or resizing failed: {str(e)}")

        file_path = f"recipe/{id}.png"
        try:
            public_url = await upload_file_to_s3(
                resized_bytes,
                file_path,
                "image/png"
            )
        except Exception as e:
            print("UPLOAD ERROR:", e)
            raise HTTPException(500, f"Upload failed: {str(e)}")

        recipes_collection.insert_one({
            "id": id,
            "name": name,
            "main_ingredients": ing_list,
            "detected_condition": cond_list,
            "how_it_improves": how_it_improves,
            "tags": tag_list,
            "image_url": public_url
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
    doc = nutritions_collection.find_one(get_nutrition_filter(id))
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
    query_filter = get_nutrition_filter(id)
    existing = nutritions_collection.find_one(query_filter)
    if not existing:
        raise HTTPException(404, "Nutrition not found")
        
    actual_id = existing["id"]
    updates = {}
    if name is not None:
        updates["name"] = name
    if main_ingredient is not None:
        updates["main ingredient"] = main_ingredient
    if detected_condition is not None:
        cond_list = clean_conditions(detected_condition)
        updates["detected conditions"] = cond_list
    if how_it_improves is not None:
        updates["how to improves"] = how_it_improves
        
    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"nutrition/{actual_id}.png"
        public_url = await upload_image(file, file_path)
        updates["image url"] = public_url
        
    if updates:
        nutritions_collection.update_one(query_filter, {"$set": updates})
        
    res = nutritions_collection.find_one(query_filter)
    res["_id"] = str(res["_id"])
    return {"message": "Nutrition updated", "data": res}


@router.delete("/nutrition/{id}")
async def delete_nutrition(id: str):
    query_filter = get_nutrition_filter(id)
    existing = nutritions_collection.find_one(query_filter)
    if not existing:
        raise HTTPException(404, "Nutrition not found")
    nutritions_collection.delete_one(query_filter)
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
    doc = foods_collection.find_one(get_food_filter(id))
    if not doc:
        raise HTTPException(404, "Food not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/food/{id}")
@router.put("/food/{id}")
async def update_food(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None),
    ingredients: Optional[str] = Form(None),
    detected_condition: Optional[str] = Form(None),
    benefits: Optional[str] = Form(None)
):
    query_filter = get_food_filter(id)
    existing = foods_collection.find_one(query_filter)
    if not existing:
        raise HTTPException(404, "Food not found")

    actual_id = existing["id"]
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

    if file:
        if not file.content_type.startswith("image/"):
            raise HTTPException(400, "Only image allowed")
        file_path = f"food/{actual_id}.png"
        public_url = await upload_image(file, file_path)
        updates["icon_url"] = public_url

    if updates:
        foods_collection.update_one(query_filter, {"$set": updates})

    res = foods_collection.find_one(query_filter)
    res["_id"] = str(res["_id"])
    return {"message": "Food updated", "data": res}


@router.delete("/food/{id}")
async def delete_food(id: str):
    query_filter = get_food_filter(id)
    existing = foods_collection.find_one(query_filter)
    if not existing:
        raise HTTPException(404, "Food not found")
    foods_collection.delete_one(query_filter)
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
    # Attempt to find by ObjectId first, otherwise by custom string id
    try:
        oid = ObjectId(id)
        doc = recipes_collection.find_one({"_id": oid})
    except Exception:
        doc = recipes_collection.find_one({"id": normalize_id(id)})
    if not doc:
        raise HTTPException(404, "Recipe not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/recipe/{id}")
async def update_recipe(
    id: str,
    file: UploadFile = File(None),
    name: Optional[str] = Form(None),
    main_ingredients: Optional[str] = Form(None),
    detected_condition: Optional[str] = Form(None),
    how_it_improves: Optional[str] = Form(None),
    tags: Optional[str] = Form(None)
):
    existing = recipes_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Recipe not found")

    updates = {}
    if name is not None:
        updates["name"] = name
    if main_ingredients is not None:
        updates["main_ingredients"] = clean_list(main_ingredients)
    if detected_condition is not None:
        updates["detected_condition"] = clean_conditions(detected_condition)
    if how_it_improves is not None:
        updates["how_it_improves"] = how_it_improves
        updates["description"] = how_it_improves
    if tags is not None:
        updates["tags"] = clean_list(tags)
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
    # Try to delete by Mongo ObjectId first
    try:
        oid = ObjectId(id)
        result = recipes_collection.delete_one({"_id": oid})
    except Exception:
        # Fallback to custom string id (normalized)
        result = recipes_collection.delete_one({"id": normalize_id(id)})
    if result.deleted_count == 0:
        raise HTTPException(404, "Recipe not found")
    return {"message": "Recipe deleted successfully"}