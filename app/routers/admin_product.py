from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from typing import Optional
from PIL import Image
import io
from app.core.s3_client import upload_file_to_s3
from app.core.mongo_client import products_collection
from app.routers.admin_auth import _get_current_admin

router = APIRouter(prefix="/admin", tags=["Admin Upload"], dependencies=[Depends(_get_current_admin)])


# =========================
# HELPERS
# =========================

def normalize_id(raw_id: str) -> str:
    return raw_id.strip().lower().replace(" ", "_")


def clean_list(data: str):
    return [x.strip().lower().replace(" ", "_") for x in data.split(",") if x]


async def upload_image(file: UploadFile, path: str) -> str:
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

        # upload to S3
        url = await upload_file_to_s3(buffer.read(), path, "image/jpeg")
        return url

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
        existing = products_collection.find_one({"id": id})
        if existing:
            raise HTTPException(400, "Product ID already exists")

        # upload image
        file_path = f"products/{id}.jpg"
        public_url = await upload_image(file, file_path)

        # insert into DB
        products_collection.insert_one({
            "id": id,
            "name": name,
            "image_url": public_url,
            "category": category.lower(),
            "tags": tags_list,          
            "concerns": concerns_list,  
            "priority": priority
        })

        return {
            "message": "Product uploaded successfully",
            "id": id,
            "image_url": public_url
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── PRODUCT CRUD (GET, PUT, DELETE) ───────────────────────────────────────────

@router.get("/product")
async def list_products():
    cursor = products_collection.find({}).sort("priority", -1)
    results = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


@router.get("/product/{id}")
async def get_product(id: str):
    doc = products_collection.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Product not found")
    doc["_id"] = str(doc["_id"])
    return doc


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
    existing = products_collection.find_one({"id": id})
    if not existing:
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
        public_url = await upload_image(file, file_path)
        updates["image_url"] = public_url
        
    if updates:
        products_collection.update_one({"id": id}, {"$set": updates})
        
    res = products_collection.find_one({"id": id})
    res["_id"] = str(res["_id"])
    return {"message": "Product updated successfully", "data": res}


@router.delete("/product/{id}")
async def delete_product(id: str):
    existing = products_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Product not found")
    products_collection.delete_one({"id": id})
    return {"message": "Product deleted successfully"}