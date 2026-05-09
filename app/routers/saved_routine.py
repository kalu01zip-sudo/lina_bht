from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from app.core.supabase_client import supabase
from app.routers.auth import CurrentUser


router = APIRouter(
    prefix="/routine",
    tags=["Saved Routine"]
)


# =========================
# REQUEST MODELS
# =========================

class RoutineItem(BaseModel):
    time: str
    phase: str

    product_category: str
    product_name: str | None = None
    product_url: str | None = None

    why: str


class SaveRoutineRequest(BaseModel):
    scan_id: str
    routines: List[RoutineItem]


# =========================
# SAVE ROUTINE
# =========================

@router.post("/save")
async def save_routine(
    body: SaveRoutineRequest,
    current_user: CurrentUser
):

    try:
        rows = []

        for item in body.routines:

            rows.append({
                "user_id": str(current_user["_id"]),
                "scan_id": body.scan_id,

                "time": item.time,
                "phase": item.phase,

                "product_category": item.product_category,
                "product_name": item.product_name,
                "product_url": item.product_url,

                "why": item.why
            })

        result = supabase.table("saved_routines") \
            .insert(rows) \
            .execute()

        return {
            "success": True,
            "saved_count": len(rows),
            "data": result.data
        }

    except Exception as e:
        print("SAVE ROUTINE ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to save routine"
        )