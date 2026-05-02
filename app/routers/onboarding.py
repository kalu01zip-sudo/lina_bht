from fastapi import APIRouter, Depends
from app.schemas.onboarding import PersonalInfoRequest
from app.core.auth_utils import get_current_user
from app.core.database import users_col
from bson import ObjectId

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


@router.post("/personal_info")
async def save_personal_info(
    data: PersonalInfoRequest,
    current_user=Depends(get_current_user)
):
    user_id = current_user["sub"]

    await users_col().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "country": data.country,
                "languages": data.language,
                "date_of_birth": data.date_of_birth.isoformat(),
                "gender": data.gender,
                "onboarding_step": "personal_info"
            }
        }
    )

    return {"message": "Personal info saved successfully"}