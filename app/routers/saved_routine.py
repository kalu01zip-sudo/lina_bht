from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    Field
)
from typing import List

from app.core.supabase_client import (
    supabase
)

from app.routers.auth import (
    CurrentUser
)

from app.services.routine_draft_service import (
    get_routine_drafts_by_ids,
    missing_routine_ids
)


router = APIRouter(
    prefix="/routine",
    tags=["Routine"]
)


# =========================
# REQUEST MODEL
# =========================

class SaveRoutineRequest(
    BaseModel
):

    routine_step_id: List[str] = Field(
        ...,
        min_length=1
    )


# =========================
# SAVE GENERATED ROUTINE STEPS
# =========================

@router.post("/save")
async def save_routine(
    body: SaveRoutineRequest,
    current_user: CurrentUser
):

    try:

        user_id = str(
            current_user["_id"]
        )

        drafts = get_routine_drafts_by_ids(
            user_id=user_id,
            routine_ids=body.routine_step_id
        )

        missing = missing_routine_ids(
            routine_ids=body.routine_step_id,
            drafts=drafts
        )

        if missing:

            raise HTTPException(
                status_code=404,
                detail={
                    "message": "Routine step draft not found",
                    "missing_ids": missing
                }
            )

        rows = []

        for draft in drafts:

            rows.append({
                "id": draft["routine_id"],
                "user_id": user_id,
                "scan_id": draft.get(
                    "scan_id"
                ) or draft["routine_id"],
                "time": draft.get(
                    "time"
                ),
                "phase": draft.get(
                    "phase"
                ),
                "product_category": draft.get(
                    "product_category"
                ),
                "product_name": draft.get(
                    "product_name"
                ),
                "product_url": draft.get(
                    "product_url"
                ),
                "why": draft.get(
                    "why"
                )
            })

        result = supabase.table(
            "saved_routines"
        ).insert(
            rows
        ).execute()

        return {
            "success": True,
            "saved_count": len(rows),
            "routine_step_id": body.routine_step_id,
            "data": result.data
        }

    except HTTPException:

        raise

    except Exception as e:

        print("SAVE ROUTINE ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to save routine"
        )


# =========================
# DELETE ROUTINE STEP
# =========================

@router.delete("/step/{step_id}")
async def delete_routine_step(
    step_id: str,
    current_user: CurrentUser
):

    try:

        user_id = str(
            current_user["_id"]
        )

        # Verify the step exists and belongs to the current user
        existing_step = supabase.table(
            "saved_routines"
        ).select(
            "*"
        ).eq(
            "id", step_id
        ).eq(
            "user_id", user_id
        ).execute()

        if not existing_step.data:

            raise HTTPException(
                status_code=404,
                detail="Routine step not found or does not belong to this user"
            )

        # Delete the step
        result = supabase.table(
            "saved_routines"
        ).delete().eq(
            "id", step_id
        ).execute()

        return {
            "success": True,
            "message": "Routine step deleted successfully",
            "deleted_step_id": step_id
        }

    except HTTPException:

        raise

    except Exception as e:

        print("DELETE ROUTINE STEP ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to delete routine step"
        )


# =========================
# GET ALL SAVED ROUTINES
# =========================

@router.get("/all")
async def get_all_saved_routines(
    current_user: CurrentUser
):

    try:

        user_id = str(
            current_user["_id"]
        )

        # Fetch all routine steps for the current user
        result = supabase.table(
            "saved_routines"
        ).select(
            "*"
        ).eq(
            "user_id", user_id
        ).order(
            "time",
            desc=False
        ).execute()

        return {
            "success": True,
            "count": len(result.data),
            "data": result.data
        }

    except Exception as e:

        print("GET ALL SAVED ROUTINES ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to fetch saved routines"
        )
