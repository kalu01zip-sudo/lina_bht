# app/routers/lia.py
"""
Lia — Notification & Coaching API endpoints.

Endpoints:
  GET    /lia/notifications           → Fetch user's notifications
  GET    /lia/notifications/unread-count → Count of unread
  PATCH  /lia/notifications/{id}/read → Mark one as read
  PATCH  /lia/notifications/read-all  → Mark all as read
  POST   /lia/coaching                → On-demand coaching message
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from app.routers.auth import CurrentUser

from app.services.lia_notification_service import (
    get_user_notifications,
    get_unread_count,
    mark_read,
    mark_all_read,
)

from app.services.lia_coaching_engine import (
    generate_on_demand_coaching,
)

from app.core.mongo_client import scan_collection
from app.core.supabase_client import supabase


router = APIRouter(
    prefix="/lia",
    tags=["Lia AI Coach"],
)


# ══════════════════════════════════════════════════════════════════════════════
#  GET NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/notifications")
async def get_notifications(
    current_user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    unread_only: bool = Query(default=False),
):
    """
    Fetch the user's Lia notifications, newest first.

    Query params:
      - limit: max number of notifications (default 20, max 100)
      - unread_only: if true, only return unread notifications
    """
    user_id = str(current_user["_id"])

    notifications = get_user_notifications(
        user_id=user_id,
        limit=limit,
        unread_only=unread_only,
    )

    return {
        "success": True,
        "count": len(notifications),
        "notifications": notifications,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  UNREAD COUNT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/notifications/unread-count")
async def unread_notification_count(
    current_user: CurrentUser,
):
    """Returns the count of unread notifications."""
    user_id = str(current_user["_id"])
    count = get_unread_count(user_id)

    return {
        "success": True,
        "unread_count": count,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MARK READ
# ══════════════════════════════════════════════════════════════════════════════

@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: CurrentUser,
):
    """Mark a single notification as read."""
    user_id = str(current_user["_id"])

    success = mark_read(notification_id, user_id)

    if not success:
        raise HTTPException(
            status_code=404,
            detail="Notification not found",
        )

    return {
        "success": True,
        "notification_id": notification_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MARK ALL READ
# ══════════════════════════════════════════════════════════════════════════════

@router.patch("/notifications/read-all")
async def mark_all_notifications_read(
    current_user: CurrentUser,
):
    """Mark all notifications as read."""
    user_id = str(current_user["_id"])

    count = mark_all_read(user_id)

    return {
        "success": True,
        "marked_read": count,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ON-DEMAND COACHING
# ══════════════════════════════════════════════════════════════════════════════

class CoachingRequest(BaseModel):
    context: str = "general"
    # Options: "morning_reminder", "evening_reminder",
    #          "weekly_check_in", "post_scan", "general"


@router.post("/coaching")
async def get_coaching_message(
    body: CoachingRequest,
    current_user: CurrentUser,
):
    """
    Generate a personalized coaching message on demand.

    This does NOT save to notification history — it's for
    the chat/coaching screen. Use context to specify the type:
      - general: general skincare tip
      - morning_reminder: morning routine motivation
      - evening_reminder: evening routine motivation
      - weekly_check_in: progress summary
      - post_scan: advice based on latest scan
    """
    user_id = str(current_user["_id"])

    # Fetch last scan
    last_scan = scan_collection.find_one(
        {"user_id": user_id},
        sort=[("created_at", -1)],
    )

    # Fetch routine steps
    try:
        routine_response = supabase.table("saved_routines") \
            .select("*") \
            .eq("user_id", user_id) \
            .execute()
        routine_steps = routine_response.data or []
    except Exception:
        routine_steps = []

    result = generate_on_demand_coaching(
        user_doc=current_user,
        last_scan=last_scan,
        routine_steps=routine_steps,
        context_type=body.context,
    )

    return {
        "success": True,
        **result,
    }
