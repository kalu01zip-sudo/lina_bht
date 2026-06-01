import asyncio
from datetime import date, datetime
from typing import Any

from bson import ObjectId
from fastapi import APIRouter

from app.core.mongo_client import scalp_scan_collection, scan_collection
from app.core.supabase_client import supabase
from app.routers.auth import CurrentUser
from app.routers.saved_routine import _reset_stale_weekly_steps

router = APIRouter(prefix="/homepage", tags=["Homepage"])


def _json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _format_scan(doc: dict | None) -> dict | None:
    if not doc:
        return None
    
    analysis = doc.get("analysis", {})

    return {
        "analysis": {
            "overall_score": analysis.get("overall_score"),
            "checked_area": analysis.get("checked_area", {})
        }
    }


def _fetch_last_scans(user_id: str) -> tuple[dict | None, dict | None]:
    face_scan = scan_collection.find_one(
        {"user_id": user_id},
        projection={"analysis": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    scalp_scan = scalp_scan_collection.find_one(
        {"user_id": user_id},
        projection={"analysis": 1, "created_at": 1},
        sort=[("created_at", -1)],
    )
    return face_scan, scalp_scan


def _fetch_routine_summary(user_id: str) -> dict:
    result = supabase.table(
        "saved_routines"
    ).select(
        "id, time, product_name, is_completed, completed_at"
    ).eq(
        "user_id", user_id
    ).order(
        "time",
        desc=False
    ).execute()

    rows = _reset_stale_weekly_steps(
        result.data or []
    )

    return {
        "count": len(rows),
        "completed": sum(1 for row in rows if row.get("is_completed")),
        "data": [
            {
                "time": row.get("time"),
                "product_name": row.get("product_name"),
            }
            for row in rows
        ],
    }


@router.get("/scans")
async def get_homepage_scans(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    (face_scan, scalp_scan), routine = await asyncio.gather(
        asyncio.to_thread(_fetch_last_scans, user_id),
        asyncio.to_thread(_fetch_routine_summary, user_id),
    )

    return {
        "face_scan": _format_scan(face_scan),
        "scalp_scan": _format_scan(scalp_scan),
        "routine": routine,
    }
