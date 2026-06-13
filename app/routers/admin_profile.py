from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from typing import Optional
import uuid
from pydantic import BaseModel, Field
from app.core.s3_client import upload_file_to_s3
from app.routers.admin_auth import _get_current_admin

router = APIRouter(
    prefix="/admin/profile",
    tags=["Admin Authentication"],
    dependencies=[Depends(_get_current_admin)],
)

class AdminProfileResponse(BaseModel):
    id: str = Field(..., description="Admin unique identifier")
    email: str = Field(..., description="Admin email address")
    full_name: Optional[str] = Field(None, description="Admin display name")
    avatar_url: Optional[str] = Field(None, description="URL of the admin avatar image")

@router.get(
    "/me",
    response_model=AdminProfileResponse,
    summary="Get admin profile (name & avatar)",
)
async def get_admin_profile(current_admin: dict = Depends(_get_current_admin)):
    return AdminProfileResponse(
        id=str(current_admin.get("_id")),
        email=current_admin.get("email"),
        full_name=current_admin.get("full_name"),
        avatar_url=current_admin.get("avatar_url"),
    )

@router.put(
    "/me",
    response_model=AdminProfileResponse,
    summary="Update admin name and avatar",
)
async def update_admin_profile(
    avatar: Optional[UploadFile] = File(None),
    full_name: Optional[str] = Form(None),
    current_admin: dict = Depends(_get_current_admin),
):
    from app.core.database import get_db

    admins_col = get_db()["admins"]
    updates = {}
    if full_name is not None:
        updates["full_name"] = full_name
    if avatar:
        if not avatar.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are allowed for avatar.")
        extension = avatar.filename.split(".")[-1]
        filename = f"{current_admin['_id']}_{uuid.uuid4().hex}.{extension}"
        file_bytes = await avatar.read()
        try:
            avatar_url = await upload_file_to_s3(
                file_bytes,
                f"admin_avatars/{filename}",
                avatar.content_type or "image/png",
            )
            updates["avatar_url"] = avatar_url
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Avatar upload failed: {str(e)}")
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided.")
    await admins_col.update_one({"_id": current_admin["_id"]}, {"$set": updates})
    admin = await admins_col.find_one({"_id": current_admin["_id"]})
    return AdminProfileResponse(
        id=str(admin.get("_id")),
        email=admin.get("email"),
        full_name=admin.get("full_name"),
        avatar_url=admin.get("avatar_url"),
    )
