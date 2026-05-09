from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from app.core.supabase_client import supabase
import uuid

router = APIRouter(
    prefix="/admin",
    tags=["Admin"]
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