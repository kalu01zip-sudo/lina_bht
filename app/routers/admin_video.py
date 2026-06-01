from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.s3_client import upload_file_to_s3, delete_file_from_s3
from app.core.mongo_client import routine_videos_collection
import uuid
from typing import Optional

router = APIRouter(
    prefix="/admin",
    tags=["Admin Upload"]
)


def clean_tags(tags: str):
    return [
        x.strip().lower().replace(" ", "_")
        for x in tags.split(",")
        if x.strip()
    ]


@router.post("/video")
async def upload_routine_video(
    file: UploadFile = File(...),
    title: str = Form(...),
    tags: str = Form(...),
    phase: str = Form(...),
    product_category: str = Form(...),
    priority: int = Form(1)
):
    try:
        video_id = str(uuid.uuid4())
        video_bytes = await file.read()

        # Upload to S3
        s3_path = f"routine-videos/{video_id}.mp4"
        video_url = await upload_file_to_s3(
            video_bytes,
            s3_path,
            file.content_type or "video/mp4"
        )

        # Save metadata to MongoDB
        doc = {
            "id": video_id,
            "title": title,
            "video_url": video_url,
            "tags": clean_tags(tags),
            "phase": phase,
            "product_category": product_category,
            "priority": priority
        }
        routine_videos_collection.insert_one(doc)

        return {
            "success": True,
            "video_url": video_url
        }

    except Exception as e:
        print("VIDEO UPLOAD ERROR:", e)
        raise HTTPException(
            status_code=500,
            detail=f"Video upload failed: {str(e)}"
        )


# ── ROUTINE VIDEO CRUD (GET, PUT, DELETE) ─────────────────────────────────────

@router.get("/video")
async def list_videos():
    cursor = routine_videos_collection.find({}).sort("priority", -1)
    results = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


@router.get("/video/{id}")
async def get_video(id: str):
    doc = routine_videos_collection.find_one({"id": id})
    if not doc:
        raise HTTPException(404, "Video not found")
    doc["_id"] = str(doc["_id"])
    return doc


@router.put("/video/{id}")
async def update_video(
    id: str,
    file: UploadFile = File(None),
    title: Optional[str] = Form(None, examples=[""]),
    tags: Optional[str] = Form(None, examples=[""]),
    phase: Optional[str] = Form(None, examples=[""]),
    product_category: Optional[str] = Form(None, examples=[""]),
    priority: Optional[int] = Form(None)
):
    existing = routine_videos_collection.find_one({"id": id})
    if not existing:
        raise HTTPException(404, "Video not found")

    updates = {}
    if title is not None:
        updates["title"] = title
    if tags is not None:
        updates["tags"] = clean_tags(tags)
    if phase is not None:
        updates["phase"] = phase
    if product_category is not None:
        updates["product_category"] = product_category
    if priority is not None:
        updates["priority"] = priority

    if file:
        file_bytes = await file.read()
        s3_path = f"routine-videos/{id}.mp4"
        video_url = await upload_file_to_s3(
            file_bytes,
            s3_path,
            file.content_type or "video/mp4"
        )
        updates["video_url"] = video_url

    if updates:
        routine_videos_collection.update_one({"id": id}, {"$set": updates})

    res = routine_videos_collection.find_one({"id": id})
    res["_id"] = str(res["_id"])
    return {"message": "Video updated successfully", "data": res}


@router.delete("/video/{id}")
async def delete_video(id: str):
    existing = routine_videos_collection.find_one({"id": id}, {"id": 1, "video_url": 1})
    if not existing:
        raise HTTPException(404, "Video not found")

    # Delete from S3
    try:
        s3_path = f"routine-videos/{id}.mp4"
        await delete_file_from_s3(s3_path)
    except Exception as e:
        print(f"[WARN] S3 delete failed for video {id}: {e}")

    routine_videos_collection.delete_one({"id": id})
    return {"message": "Video deleted successfully"}