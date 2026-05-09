from fastapi import APIRouter, Depends, HTTPException
from app.schemas.onboarding import (
    PersonalInfoRequest,
    LifePhaseRequest,
    AllergiesRequest,
    SkinHairRequest,
    BudgetRequest
)
from app.core.auth_utils import get_current_user
from app.core.database import users_col
from bson import ObjectId

from app.services.text_utils import compress_text, expand_and_clean
from app.services.constants import ALLOWED_HAIR_CONCERNS, ALLOWED_SKIN_CONCERNS


router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


# ---------------- PERSONAL INFO ----------------
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
                "language": data.language,
                "date_of_birth": data.date_of_birth.isoformat(),
                "gender": data.gender,
                "onboarding_step": "personal_info"
            }
        }
    )

    return {"message": "Personal info saved successfully"}



# ---------------- LIFE PHASE ----------------
@router.post("/life_phase")
async def save_life_phase(
    data: LifePhaseRequest,
    current_user=Depends(get_current_user)
):
    user_id = current_user["sub"]

    life_phase_value = data.current_phase

    if data.current_phase == "other":
        if not data.custom_text:
            raise HTTPException(status_code=400, detail="custom_text required")
        life_phase_value = compress_text(data.custom_text)

    await users_col().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "life_phase": life_phase_value,
                "onboarding_step": "life_phase"
            }
        }
    )

    return {"message": "Life phase saved"}

@router.get("/life_phase")
async def get_life_phase(
    current_user=Depends(get_current_user)
):

    user = await users_col().find_one({
        "_id": ObjectId(current_user["sub"])
    })

    return {
        "life_phase": user.get("life_phase")
    }

@router.patch("/life_phase")
async def patch_life_phase(

    data: LifePhaseRequest,

    current_user=Depends(get_current_user)
):

    user_id = current_user["sub"]

    life_phase_value = data.current_phase

    if data.current_phase == "other":

        if not data.custom_text:

            raise HTTPException(
                status_code=400,
                detail="custom_text required"
            )

        life_phase_value = compress_text(
            data.custom_text
        )

    await users_col().update_one(

        {"_id": ObjectId(user_id)},

        {
            "$set": {
                "life_phase": life_phase_value
            }
        }
    )

    return {
        "message": "Life phase updated"
    }



# ---------------- ALLERGIES ----------------
@router.post("/allergies")
async def save_allergies(
    data: AllergiesRequest,
    current_user=Depends(get_current_user)
):
    user_id = current_user["sub"]

    allergies = data.allergies

    if "none" in allergies and len(allergies) > 1:
        raise HTTPException(status_code=400, detail="'none' cannot be combined")

    final_allergies = []

    if "none" in allergies:
        final_allergies = []
    else:
        for item in allergies:
            if item == "other":
                if not data.custom_text:
                    raise HTTPException(status_code=400, detail="custom_text required")
                final_allergies.append(compress_text(data.custom_text))
            else:
                final_allergies.append(item)

    await users_col().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "allergies": final_allergies,
                "onboarding_step": "allergies"
            }
        }
    )

    return {"message": "Allergies saved"}

@router.get("/allergies")
async def get_allergies(
    current_user=Depends(get_current_user)
):

    user = await users_col().find_one({
        "_id": ObjectId(current_user["sub"])
    })

    return {
        "allergies": user.get("allergies", [])
    }

@router.patch("/allergies")
async def patch_allergies(

    data: AllergiesRequest,

    current_user=Depends(get_current_user)
):

    user_id = current_user["sub"]

    allergies = data.allergies

    if "none" in allergies and len(allergies) > 1:

        raise HTTPException(
            status_code=400,
            detail="'none' cannot be combined"
        )

    final_allergies = []

    if "none" in allergies:

        final_allergies = []

    else:

        for item in allergies:

            if item == "other":

                if not data.custom_text:

                    raise HTTPException(
                        status_code=400,
                        detail="custom_text required"
                    )

                final_allergies.append(
                    compress_text(data.custom_text)
                )

            else:

                final_allergies.append(item)

    await users_col().update_one(

        {"_id": ObjectId(user_id)},

        {
            "$set": {
                "allergies": final_allergies
            }
        }
    )

    return {
        "message": "Allergies updated"
    }



# ---------------- SKIN + HAIR ----------------
@router.post("/skin_hair")
async def save_skin_hair(
    data: SkinHairRequest,
    current_user=Depends(get_current_user)
):
    user_id = current_user["sub"]

    # -------- SKIN --------
    expanded_skin = expand_and_clean(data.skin_concerns)

    final_skin = []

    for item in expanded_skin:
        if item == "other":
            if not data.skin_other:
                raise HTTPException(status_code=400, detail="skin_other required")
            
            final_skin.append(compress_text(data.skin_other))  # only this
        else:
            if item not in ALLOWED_SKIN_CONCERNS:
                raise HTTPException(status_code=400, detail=f"Invalid skin concern: {item}")
            
            final_skin.append(item)

    # handle none
    if "none" in final_skin and len(final_skin) > 1:
        raise HTTPException(status_code=400, detail="Invalid combination")

    if "none" in final_skin:
        final_skin = []

    # -------- HAIR --------
    expanded_hair = expand_and_clean(data.hair_concerns)

    final_hair = []

    for item in expanded_hair:
        if item == "other":
            if not data.hair_other:
                raise HTTPException(status_code=400, detail="hair_other required")
            
            final_hair.append(compress_text(data.hair_other))
        else:
            if item not in ALLOWED_HAIR_CONCERNS:
                raise HTTPException(status_code=400, detail=f"Invalid hair concern: {item}")
            
            final_hair.append(item)

    if "none" in final_hair and len(final_hair) > 1:
        raise HTTPException(status_code=400, detail="Invalid combination")

    if "none" in final_hair:
        final_hair = []
    # -------- SAVE --------
    await users_col().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "skin_type": data.skin_type,
                "skin_concerns": final_skin,
                "hair_type": data.hair_type,
                "hair_concerns": final_hair,
                "onboarding_step": "skin_hair"
            }
        }
    )

    return {"message": "Skin & Hair saved"}

@router.get("/skin_hair")
async def get_skin_hair(
    current_user=Depends(get_current_user)
):

    user = await users_col().find_one({
        "_id": ObjectId(current_user["sub"])
    })

    return {

        "skin_type":
            user.get("skin_type"),

        "skin_concerns":
            user.get("skin_concerns", []),

        "hair_type":
            user.get("hair_type"),

        "hair_concerns":
            user.get("hair_concerns", [])
    }

@router.patch("/skin_hair")
async def patch_skin_hair(

    data: SkinHairRequest,

    current_user=Depends(get_current_user)
):

    user_id = current_user["sub"]

    # ==========================
    # SKIN
    # ==========================

    expanded_skin = expand_and_clean(
        data.skin_concerns
    )

    final_skin = []

    for item in expanded_skin:

        if item == "other":

            if not data.skin_other:

                raise HTTPException(
                    status_code=400,
                    detail="skin_other required"
                )

            final_skin.append(
                compress_text(data.skin_other)
            )

        else:

            if item not in ALLOWED_SKIN_CONCERNS:

                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid skin concern: {item}"
                )

            final_skin.append(item)

    if "none" in final_skin and len(final_skin) > 1:

        raise HTTPException(
            status_code=400,
            detail="Invalid combination"
        )

    if "none" in final_skin:
        final_skin = []

    # ==========================
    # HAIR
    # ==========================

    expanded_hair = expand_and_clean(
        data.hair_concerns
    )

    final_hair = []

    for item in expanded_hair:

        if item == "other":

            if not data.hair_other:

                raise HTTPException(
                    status_code=400,
                    detail="hair_other required"
                )

            final_hair.append(
                compress_text(data.hair_other)
            )

        else:

            if item not in ALLOWED_HAIR_CONCERNS:

                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid hair concern: {item}"
                )

            final_hair.append(item)

    if "none" in final_hair and len(final_hair) > 1:

        raise HTTPException(
            status_code=400,
            detail="Invalid combination"
        )

    if "none" in final_hair:
        final_hair = []

    # ==========================
    # SAVE
    # ==========================

    await users_col().update_one(

        {"_id": ObjectId(user_id)},

        {
            "$set": {

                "skin_type": data.skin_type,

                "skin_concerns": final_skin,

                "hair_type": data.hair_type,

                "hair_concerns": final_hair
            }
        }
    )

    return {
        "message": "Skin & Hair updated"
    }

#------------------ BUDGET ----------------
@router.post("/budget")
async def save_budget(
    data: BudgetRequest,
    current_user=Depends(get_current_user)
):
    user_id = current_user["sub"]

    await users_col().update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "budget": data.budget,
                "onboarding_step": "completed",
                "onboarding_completed": True
            }
        }
    )

    return {"message": "Onboarding completed"}