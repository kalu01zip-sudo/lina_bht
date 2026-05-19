from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase
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

        path = f"{video_id}.mp4"

        video_bytes = await file.read()

        supabase.storage \
            .from_("routine-videos") \
            .upload(
                path,
                video_bytes,
                file_options={
                    "content-type": file.content_type
                }
            )

        video_url = supabase.storage \
            .from_("routine-videos") \
            .get_public_url(path)

        supabase.table("routine_videos") \
            .insert({

                "id": video_id,

                "title": title,

                "video_url": video_url,

                "tags": clean_tags(tags),

                "phase": phase,

                "product_category": product_category,

                "priority": priority
            }) \
            .execute()

        return {
            "success": True,
            "video_url": video_url
        }

    except Exception as e:

        print("VIDEO UPLOAD ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Video upload failed"
        )


# ── ROUTINE VIDEO CRUD (GET, PUT, DELETE) ─────────────────────────────────────

@router.get("/video")
async def list_videos():
    res = supabase.table("routine_videos").select("*").order("priority", desc=True).execute()
    return res.data


@router.get("/video/{id}")
async def get_video(id: str):
    res = supabase.table("routine_videos").select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(404, "Video not found")
    return res.data[0]


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
    existing = supabase.table("routine_videos").select("*").eq("id", id).execute()
    if not existing.data:
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
        file_path = f"{id}.mp4"
        supabase.storage.from_("routine-videos").upload(
            file_path,
            file_bytes,
            file_options={
                "content-type": file.content_type,
                "x-upsert": "true"
            }
        )
        url_res = supabase.storage.from_("routine-videos").get_public_url(file_path)
        if isinstance(url_res, dict):
            updates["video_url"] = url_res.get("publicUrl")
        else:
            updates["video_url"] = url_res
            
    if updates:
        supabase.table("routine_videos").update(updates).eq("id", id).execute()
        
    res = supabase.table("routine_videos").select("*").eq("id", id).execute()
    return {"message": "Video updated successfully", "data": res.data[0]}


@router.delete("/video/{id}")
async def delete_video(id: str):
    existing = supabase.table("routine_videos").select("id").eq("id", id).execute()
    if not existing.data:
        raise HTTPException(404, "Video not found")
    supabase.table("routine_videos").delete().eq("id", id).execute()
    return {"message": "Video deleted successfully"}