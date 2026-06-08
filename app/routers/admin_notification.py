from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.mongo_client import lia_notifications_collection
from app.routers.admin_auth import CurrentAdmin
from app.services.admin_notification_settings import (
    get_notification_settings,
    save_notification_settings,
    update_delivery_settings,
    update_reminder,
)


router = APIRouter(
    prefix="/admin/notification",
    tags=["Admin Notification"],
)

ScheduleType = Literal["daily", "weekly"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TimezoneHandling = Literal["user_local_time", "server_time"]


def _admin_id(admin: dict) -> str:
    return str(admin.get("_id", ""))


def _serialize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


def _notification_query(
    trigger: str | None = None,
    user_id: str | None = None,
    unread_only: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if trigger:
        query["trigger"] = trigger
    if user_id:
        query["user_id"] = user_id
    if unread_only:
        query["is_read"] = False

    created_filter: dict[str, str] = {}
    if date_from:
        created_filter["$gte"] = date_from.astimezone(timezone.utc).isoformat()
    if date_to:
        created_filter["$lte"] = date_to.astimezone(timezone.utc).isoformat()
    if created_filter:
        query["created_at"] = created_filter

    return query


class ReminderSchedule(BaseModel):
    type: ScheduleType
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    day_of_week: Optional[Weekday] = None

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        hour, minute = [int(part) for part in value.split(":")]
        if hour > 23 or minute > 59:
            raise ValueError("time must be in HH:MM 24-hour format")
        return value

    @model_validator(mode="after")
    def validate_day(self):
        if self.type == "weekly" and not self.day_of_week:
            raise ValueError("day_of_week is required for weekly schedules")
        return self


class NotificationReminder(BaseModel):
    id: str
    title: str
    category: str
    trigger: str
    enabled: bool
    schedule: ReminderSchedule


class DeliverySettings(BaseModel):
    smart_timing: bool = True
    timezone_handling: TimezoneHandling = "user_local_time"


class NotificationSettingsUpdate(BaseModel):
    reminders: list[NotificationReminder]
    delivery_settings: DeliverySettings


class ReminderPatch(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    trigger: Optional[str] = None
    enabled: Optional[bool] = None
    schedule: Optional[ReminderSchedule] = None


class DeliverySettingsPatch(BaseModel):
    smart_timing: Optional[bool] = None
    timezone_handling: Optional[TimezoneHandling] = None


@router.get("/settings")
async def get_admin_notification_settings(current_admin: CurrentAdmin):
    """Return the saved admin notification configuration."""
    return {
        "success": True,
        "settings": get_notification_settings(),
    }


@router.put("/settings")
async def save_admin_notification_settings(
    body: NotificationSettingsUpdate,
    current_admin: CurrentAdmin,
):
    """Save the full notification settings screen configuration."""
    settings = save_notification_settings(
        body.model_dump(),
        admin_id=_admin_id(current_admin),
    )
    return {
        "success": True,
        "message": "Notification configuration saved.",
        "settings": settings,
    }


@router.patch("/reminders/{reminder_id}")
async def patch_admin_notification_reminder(
    reminder_id: str,
    body: ReminderPatch,
    current_admin: CurrentAdmin,
):
    """Update one default reminder, useful for individual toggle changes."""
    reminder = update_reminder(
        reminder_id,
        body.model_dump(exclude_unset=True),
        admin_id=_admin_id(current_admin),
    )
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found.")

    return {
        "success": True,
        "message": "Reminder updated.",
        "reminder": reminder,
    }


@router.patch("/delivery-settings")
async def patch_admin_notification_delivery_settings(
    body: DeliverySettingsPatch,
    current_admin: CurrentAdmin,
):
    """Update smart timing or timezone handling settings."""
    delivery_settings = update_delivery_settings(
        body.model_dump(exclude_unset=True),
        admin_id=_admin_id(current_admin),
    )
    return {
        "success": True,
        "message": "Delivery settings updated.",
        "delivery_settings": delivery_settings,
    }


@router.get("/history")
async def get_admin_notification_history(
    current_admin: CurrentAdmin,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    trigger: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    unread_only: bool = Query(default=False),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
):
    """Paginated list of Lia notifications that have been generated."""
    query = _notification_query(
        trigger=trigger,
        user_id=user_id,
        unread_only=unread_only,
        date_from=date_from,
        date_to=date_to,
    )
    total = lia_notifications_collection.count_documents(query)
    cursor = (
        lia_notifications_collection.find(query)
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )
    notifications = [_serialize_doc(doc) for doc in cursor]

    return {
        "success": True,
        "notifications": notifications,
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(notifications),
    }


@router.get("/stats")
async def get_admin_notification_stats(
    current_admin: CurrentAdmin,
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
):
    """Summary counters for the admin notification section."""
    query = _notification_query(date_from=date_from, date_to=date_to)
    now = datetime.now(timezone.utc)
    last_24h_query = {
        **query,
        "created_at": {
            **query.get("created_at", {}),
            "$gte": (now - timedelta(days=1)).isoformat(),
        },
    }

    by_trigger_pipeline = [
        {"$match": query},
        {"$group": {"_id": "$trigger", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    by_trigger = [
        {"trigger": item["_id"], "count": item["count"]}
        for item in lia_notifications_collection.aggregate(by_trigger_pipeline)
    ]

    return {
        "success": True,
        "total": lia_notifications_collection.count_documents(query),
        "unread": lia_notifications_collection.count_documents({
            **query,
            "is_read": False,
        }),
        "last_24h": lia_notifications_collection.count_documents(last_24h_query),
        "by_trigger": by_trigger,
    }


@router.post("/scheduler/reload")
async def reload_admin_notification_scheduler(current_admin: CurrentAdmin):
    """Reload Lia scheduled jobs after notification settings are changed."""
    from app.services.lia_scheduler import start_scheduler, stop_scheduler

    stop_scheduler()
    start_scheduler()
    return {
        "success": True,
        "message": "Lia notification scheduler reloaded.",
        "settings": get_notification_settings(),
    }


@router.patch("/{notification_id}/toggle-read")
async def toggle_admin_notification_read(
    notification_id: str,
    current_admin: CurrentAdmin,
):
    """Toggle the is_read status of a generated notification (read <-> unread)."""
    # 1. Try finding by the UUID string "id"
    doc = lia_notifications_collection.find_one({"id": notification_id})

    # 2. If not found, try finding by MongoDB "_id" (ObjectId)
    if not doc:
        from bson import ObjectId
        try:
            doc = lia_notifications_collection.find_one({"_id": ObjectId(notification_id)})
        except Exception:
            pass

    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"Notification '{notification_id}' not found.",
        )

    # 3. Toggle is_read status
    current_status = doc.get("is_read", False)
    new_status = not current_status

    lia_notifications_collection.update_one(
        {"_id": doc["_id"]},
        {"$set": {"is_read": new_status}}
    )

    return {
        "success": True,
        "message": f"Notification read status toggled to {new_status}.",
        "notification_id": notification_id,
        "is_read": new_status,
    }


@router.patch("/read-all")
async def mark_all_admin_notifications_read(
    current_admin: CurrentAdmin,
):
    """Mark all unread notifications as read."""
    res = lia_notifications_collection.update_many(
        {"is_read": False},
        {"$set": {"is_read": True}}
    )

    return {
        "success": True,
        "message": f"Marked {res.modified_count} notifications as read.",
        "marked_read": res.modified_count,
    }


