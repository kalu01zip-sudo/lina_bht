from datetime import datetime, timezone

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


def _parse_completed_at(value: str | None) -> datetime | None:

    if not value:

        return None

    try:

        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(
            timezone.utc
        )

    except ValueError:

        return None


def _reset_stale_weekly_steps(rows: list[dict]) -> list[dict]:

    now = datetime.now(
        timezone.utc
    )
    stale_ids = []

    for row in rows:

        if row.get("time") != "weekly" or not row.get("is_completed"):

            continue

        completed_at = _parse_completed_at(
            row.get("completed_at")
        )

        if completed_at and (now - completed_at).days >= 7:

            stale_ids.append(
                row["id"]
            )
            row["is_completed"] = False
            row["completed_at"] = None

    if stale_ids:

        supabase.table(
            "saved_routines"
        ).update({
            "is_completed": False,
            "completed_at": None
        }).in_(
            "id", stale_ids
        ).execute()

    return rows


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
                ),
                "is_completed": False,
                "completed_at": None
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
# MARK ROUTINE STEP COMPLETE
# =========================

@router.patch("/step/{step_id}/complete")
async def mark_step_complete(
    step_id: str,
    current_user: CurrentUser
):

    try:

        user_id = str(
            current_user["_id"]
        )

        existing_step = supabase.table(
            "saved_routines"
        ).select(
            "id, user_id, time, is_completed"
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

        completed_at = datetime.now(
            timezone.utc
        ).isoformat()

        result = supabase.table(
            "saved_routines"
        ).update({
            "is_completed": True,
            "completed_at": completed_at
        }).eq(
            "id", step_id
        ).eq(
            "user_id", user_id
        ).execute()

        return {
            "success": True,
            "step_id": step_id,
            "is_completed": True,
            "completed_at": completed_at,
            "data": result.data[0] if result.data else None
        }

    except HTTPException:

        raise

    except Exception as e:

        print("MARK ROUTINE STEP COMPLETE ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to mark routine step complete"
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

        data = _reset_stale_weekly_steps(
            result.data or []
        )

        return {
            "success": True,
            "count": len(data),
            "data": data
        }

    except Exception as e:

        print("GET ALL SAVED ROUTINES ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to fetch saved routines"
        )
