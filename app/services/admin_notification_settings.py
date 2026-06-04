from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.core.mongo_client import db


COLLECTION_NAME = "admin_notification_settings"
SETTINGS_ID = "default"

notification_settings_collection = db[COLLECTION_NAME]


DEFAULT_NOTIFICATION_SETTINGS: dict[str, Any] = {
    "reminders": [
        {
            "id": "morning_routine",
            "title": "Morning Routine",
            "category": "routine",
            "trigger": "morning_routine",
            "enabled": True,
            "schedule": {
                "type": "daily",
                "time": "08:00",
                "day_of_week": None,
            },
        },
        {
            "id": "night_routine",
            "title": "Night Routine",
            "category": "routine",
            "trigger": "evening_routine",
            "enabled": True,
            "schedule": {
                "type": "daily",
                "time": "21:00",
                "day_of_week": None,
            },
        },
        {
            "id": "weekly_scan_reminder",
            "title": "Weekly Scan Reminder",
            "category": "scan",
            "trigger": "weekly_progress",
            "enabled": True,
            "schedule": {
                "type": "weekly",
                "time": "10:00",
                "day_of_week": "sun",
            },
        },
        {
            "id": "stress_check_in",
            "title": "Stress Check-in",
            "category": "wellness",
            "trigger": "stress_check_in",
            "enabled": False,
            "schedule": {
                "type": "weekly",
                "time": "17:00",
                "day_of_week": "fri",
            },
        },
    ],
    "delivery_settings": {
        "smart_timing": True,
        "timezone_handling": "user_local_time",
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_reminder(default: dict[str, Any], saved: dict[str, Any] | None) -> dict[str, Any]:
    reminder = deepcopy(default)
    if not saved:
        return reminder

    for key in ("title", "category", "trigger", "enabled"):
        if key in saved:
            reminder[key] = saved[key]

    schedule = deepcopy(reminder.get("schedule", {}))
    schedule.update(saved.get("schedule") or {})
    reminder["schedule"] = schedule
    return reminder


def _merge_with_defaults(saved: dict[str, Any] | None) -> dict[str, Any]:
    defaults = deepcopy(DEFAULT_NOTIFICATION_SETTINGS)
    saved = saved or {}
    saved_by_id = {
        reminder.get("id"): reminder
        for reminder in saved.get("reminders", [])
        if reminder.get("id")
    }

    merged = {
        "_id": SETTINGS_ID,
        "reminders": [
            _merge_reminder(reminder, saved_by_id.get(reminder["id"]))
            for reminder in defaults["reminders"]
        ],
        "delivery_settings": {
            **defaults["delivery_settings"],
            **(saved.get("delivery_settings") or {}),
        },
        "created_at": saved.get("created_at"),
        "updated_at": saved.get("updated_at"),
        "updated_by": saved.get("updated_by"),
    }
    return merged


def get_notification_settings() -> dict[str, Any]:
    saved = notification_settings_collection.find_one({"_id": SETTINGS_ID})
    settings = _merge_with_defaults(saved)
    settings["id"] = settings.pop("_id")
    return settings


def save_notification_settings(
    payload: dict[str, Any],
    admin_id: str | None = None,
) -> dict[str, Any]:
    current = notification_settings_collection.find_one({"_id": SETTINGS_ID})
    merged = _merge_with_defaults({**(current or {}), **payload})
    now = _now_iso()

    doc = {
        **merged,
        "_id": SETTINGS_ID,
        "created_at": merged.get("created_at") or now,
        "updated_at": now,
        "updated_by": admin_id,
    }

    notification_settings_collection.replace_one(
        {"_id": SETTINGS_ID},
        doc,
        upsert=True,
    )
    doc["id"] = doc.pop("_id")
    return doc


def update_reminder(
    reminder_id: str,
    updates: dict[str, Any],
    admin_id: str | None = None,
) -> dict[str, Any] | None:
    settings = get_notification_settings()
    reminders = settings["reminders"]

    for reminder in reminders:
        if reminder["id"] != reminder_id:
            continue

        for key in ("title", "category", "trigger", "enabled"):
            if key in updates and updates[key] is not None:
                reminder[key] = updates[key]

        if updates.get("schedule") is not None:
            reminder["schedule"] = {
                **reminder.get("schedule", {}),
                **updates["schedule"],
            }

        saved = save_notification_settings(
            {
                "reminders": reminders,
                "delivery_settings": settings["delivery_settings"],
            },
            admin_id=admin_id,
        )
        return next(r for r in saved["reminders"] if r["id"] == reminder_id)

    return None


def update_delivery_settings(
    updates: dict[str, Any],
    admin_id: str | None = None,
) -> dict[str, Any]:
    settings = get_notification_settings()
    delivery_settings = {
        **settings["delivery_settings"],
        **{k: v for k, v in updates.items() if v is not None},
    }
    saved = save_notification_settings(
        {
            "reminders": settings["reminders"],
            "delivery_settings": delivery_settings,
        },
        admin_id=admin_id,
    )
    return saved["delivery_settings"]
