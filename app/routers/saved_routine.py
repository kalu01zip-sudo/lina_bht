from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import (
    BaseModel,
    Field
)
from typing import List

from app.core.mongo_client import (
    saved_routines_collection
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

        saved_routines_collection.update_many(
            {"id": {"$in": stale_ids}},
            {"$set": {
                "is_completed": False,
                "completed_at": None
            }}
        )

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

        saved_routines_collection.insert_many(rows)

        for row in rows:
            if "_id" in row:
                row["_id"] = str(row["_id"])

        return {
            "success": True,
            "saved_count": len(rows),
            "routine_step_id": body.routine_step_id,
            "data": rows
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

        existing_step = saved_routines_collection.find_one(
            {"id": step_id, "user_id": user_id},
            {"id": 1, "user_id": 1, "time": 1, "is_completed": 1, "_id": 0}
        )

        if not existing_step:

            raise HTTPException(
                status_code=404,
                detail="Routine step not found or does not belong to this user"
            )

        completed_at = datetime.now(
            timezone.utc
        ).isoformat()

        saved_routines_collection.update_one(
            {"id": step_id, "user_id": user_id},
            {"$set": {
                "is_completed": True,
                "completed_at": completed_at
            }}
        )

        updated_step = saved_routines_collection.find_one(
            {"id": step_id, "user_id": user_id},
            {"_id": 0}
        )

        return {
            "success": True,
            "step_id": step_id,
            "is_completed": True,
            "completed_at": completed_at,
            "data": updated_step
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
        existing_step = saved_routines_collection.find_one(
            {"id": step_id, "user_id": user_id},
            {"_id": 0}
        )

        if not existing_step:

            raise HTTPException(
                status_code=404,
                detail="Routine step not found or does not belong to this user"
            )

        # Delete the step
        saved_routines_collection.delete_one(
            {"id": step_id, "user_id": user_id}
        )

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
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=200, description="Limit the number of returned routine steps"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):

    try:

        user_id = str(
            current_user["_id"]
        )

        total = saved_routines_collection.count_documents({"user_id": user_id})

        # Fetch all routine steps for the current user
        cursor = saved_routines_collection.find(
            {"user_id": user_id},
            {"_id": 0}
        ).sort("time", 1).skip(offset).limit(limit)

        rows = list(cursor)

        data = _reset_stale_weekly_steps(rows)

        return {
            "success": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "count": len(data),
            "data": data
        }

    except Exception as e:

        print("GET ALL SAVED ROUTINES ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to fetch saved routines"
        )
