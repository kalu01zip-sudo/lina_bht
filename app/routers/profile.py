from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from app.routers.auth import CurrentUser, users_col
from datetime import datetime, timedelta
from typing import Optional
import uuid
from app.core.supabase_client import supabase


router = APIRouter(
    prefix="/profile",
    tags=["Profile"]
)


@router.get("")
async def get_profile(
    current_user: CurrentUser
):

    return {

        "avatar_url": current_user.get("avatar_url"),

        "name": current_user.get("full_name"),

        "gmail": current_user.get("email"),

        "plan": current_user.get("plan", "free"),

        "onboarding": {

            "onboarding_completed":
                current_user.get(
                    "onboarding_completed",
                    False
                ),

            "skin_type":
                current_user.get("skin_type"),

            "hair_type":
                current_user.get("hair_type"),

            "current_phase":
                current_user.get("current_phase"),

            "skin_concerns":
                current_user.get(
                    "skin_concerns",
                    []
                ),

            "hair_concerns":
                current_user.get(
                    "hair_concerns",
                    []
                ),

            "allergies":
                current_user.get(
                    "allergies",
                    []
                ),

            "budget":
                current_user.get("budget")
        }
    }

#-----------------------------Stress Level Update-----------------------------
VALID_LEVELS = [

    "very_calm",

    "calm",

    "moderate",

    "stressed",

    "highly_stressed"
]

class StressRequest(BaseModel):
    stress_level: str

@router.get("/stress")
async def get_stress_level(
    current_user: CurrentUser
):

    return {

        "stress_level":
            current_user.get(
                "stress_level",
                None
            )
    }

@router.patch("/stress")
async def update_stress_level(

    body: StressRequest,

    current_user: CurrentUser
):

    level = body.stress_level.lower()

    if level not in VALID_LEVELS:

        raise HTTPException(
            status_code=400,
            detail="Invalid stress level"
        )

    await users_col().update_one(

        {
            "_id": current_user["_id"]
        },

        {
            "$set": {
                "stress_level": level
            }
        }
    )

    return {

        "success": True,

        "stress_level": level
    }

#-----------------------------cycle syncing-----------------------------
class CycleStartRequest(BaseModel):
    period_start_date: str
    # format: YYYY-MM-DD

# @router.post("/cycle_start_date")
# async def save_period_start_date(

#     body: CycleStartRequest,

#     current_user: CurrentUser
# ):

#     try:

#         parsed_date = datetime.strptime(
#             body.period_start_date,
#             "%Y-%m-%d"
#         )

#     except Exception:

#         raise HTTPException(
#             status_code=400,
#             detail="Invalid date format. Use YYYY-MM-DD"
#         )

#     await users_col().update_one(

#         {
#             "_id": current_user["_id"]
#         },

#         {
#             "$set": {
#                 "period_start_date": body.period_start_date
#             }
#         }
#     )

#     return {

#         "success": True,

#         "period_start_date":
#             body.period_start_date
#     }


# ==========================================
# PATCH PERIOD START DATE
# ==========================================

@router.patch("/cycle_start_date")
async def update_period_start_date(

    body: CycleStartRequest,

    current_user: CurrentUser
):

    try:

        parsed_date = datetime.strptime(
            body.period_start_date,
            "%Y-%m-%d"
        )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Use YYYY-MM-DD"
        )

    await users_col().update_one(

        {
            "_id": current_user["_id"]
        },

        {
            "$set": {
                "period_start_date": body.period_start_date
            }
        }
    )

    return {

        "success": True,

        "updated_period_start_date":
            body.period_start_date
    }


# ==========================================
# GET CURRENT CYCLE PHASE
# ==========================================

@router.get("/cycle_phase")
async def get_cycle_phase(
    current_user: CurrentUser
):

    start_date = current_user.get(
        "period_start_date"
    )

    if not start_date:

        raise HTTPException(
            status_code=404,
            detail="Period start date not found"
        )

    try:

        start = datetime.strptime(
            start_date,
            "%Y-%m-%d"
        )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid stored date"
        )

    today = datetime.utcnow()

    total_days = (today - start).days

    # ======================================
    # LOOP CYCLE
    # ======================================

    cycle_day = total_days % 28

    # ======================================
    # DETERMINE PHASE
    # ======================================

    if 0 <= cycle_day <= 5:

        phase = "menstruation"

    elif 6 <= cycle_day <= 11:

        phase = "follicular"

    elif 12 <= cycle_day <= 16:

        phase = "ovulation"

    else:

        phase = "luteal"

    return {

        "cycle_day": cycle_day,

        "phase": phase,

        "period_start_date": start_date
    }  


# ==========================================
# REQUEST MODEL
# ==========================================

class ProfileEditRequest(BaseModel):

    avatar_url: Optional[str] = None

    full_name: Optional[str] = None

    gender: Optional[str] = None

    date_of_birth: Optional[str] = None

    language: Optional[str] = None

    country: Optional[str] = None



# ==========================================
# GET PROFILE
# ==========================================

@router.get("/profile_edit")
async def get_profile_edit(
    current_user: CurrentUser
):

    return {

        "avatar_url":
            current_user.get("avatar_url"),

        "full_name":
            current_user.get("full_name"),

        "email":
            current_user.get("email"),

        "gender":
            current_user.get("gender"),

        "date_of_birth":
            current_user.get("date_of_birth"),

        "language":
            current_user.get("language"),

        "country":
            current_user.get("country")
    }


# ==========================================
# PATCH PROFILE
# ==========================================

@router.patch("/profile_edit")
async def update_profile_edit(

    current_user: CurrentUser,

    avatar: Optional[UploadFile] = File(None),

    full_name: Optional[str] = Form(None),

    gender: Optional[str] = Form(None),

    date_of_birth: Optional[str] = Form(None),

    language: Optional[str] = Form(None),

    country: Optional[str] = Form(None)
):

    try:

        update_data = {}

        # ======================================
        # IMAGE UPLOAD
        # ======================================

        if avatar:

            extension = avatar.filename.split(".")[-1]

            filename = (
                f"{current_user['_id']}_{uuid.uuid4()}.{extension}"
            )

            file_bytes = await avatar.read()

            supabase.storage \
                .from_("avatars") \
                .upload(

                    filename,

                    file_bytes,

                    file_options={
                        "content-type": avatar.content_type
                    }
                )

            avatar_url = supabase.storage \
                .from_("avatars") \
                .get_public_url(filename)

            update_data["avatar_url"] = avatar_url

        # ======================================
        # TEXT FIELDS
        # ======================================

        if full_name is not None:
            update_data["full_name"] = full_name

        if gender is not None:
            update_data["gender"] = gender

        if date_of_birth is not None:
            update_data["date_of_birth"] = date_of_birth

        if language is not None:
            update_data["language"] = language

        if country is not None:
            update_data["country"] = country

        # ======================================
        # UPDATE USER
        # ======================================

        if update_data:

            await users_col().update_one(

                {
                    "_id": current_user["_id"]
                },

                {
                    "$set": update_data
                }
            )

        # ======================================
        # FETCH UPDATED USER
        # ======================================

        updated_user = await users_col().find_one({
            "_id": current_user["_id"]
        })

        return {

            "success": True,

            "profile": {

                "avatar_url":
                    updated_user.get("avatar_url"),

                "full_name":
                    updated_user.get("full_name"),

                "email":
                    updated_user.get("email"),

                "gender":
                    updated_user.get("gender"),

                "date_of_birth":
                    updated_user.get("date_of_birth"),

                "language":
                    updated_user.get("language"),

                "country":
                    updated_user.get("country")
            }
        }

    except Exception as e:

        print("PROFILE UPDATE ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to update profile"
        )