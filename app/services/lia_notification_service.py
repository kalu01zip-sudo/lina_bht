# app/services/lia_notification_service.py
"""
CRUD operations for the lia_notifications MongoDB collection.
Handles saving, fetching, reading, and anti-spam checks.
"""

import uuid
from datetime import datetime, timezone, timedelta
from app.core.mongo_client import lia_notifications_collection


def save_notification(
    user_id: str,
    trigger: str,
    title: str,
    message: str,
    data: dict = None,
) -> dict | None:
    """Insert a notification into MongoDB. Returns the inserted row."""
    try:
        doc_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        
        doc = {
            "id": doc_id,
            "user_id": user_id,
            "trigger": trigger,
            "title": title,
            "message": message,
            "data": data or {},
            "is_read": False,
            "created_at": created_at
        }
        
        lia_notifications_collection.insert_one(doc)
        doc["_id"] = str(doc["_id"])
        return doc

    except Exception as e:
        print(f"[Lia] Notification save error: {e}")
        return None


def get_user_notifications(
    user_id: str,
    limit: int = 20,
    unread_only: bool = False,
    offset: int = 0,
) -> list[dict]:
    """Fetch notifications for a user, newest first."""
    try:
        query = {"user_id": user_id}
        if unread_only:
            query["is_read"] = False

        cursor = lia_notifications_collection.find(query).sort("created_at", -1).skip(offset).limit(limit)
        
        results = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            results.append(doc)
        return results

    except Exception as e:
        print(f"[Lia] Notification fetch error: {e}")
        return []


def get_unread_count(user_id: str) -> int:
    """Count unread notifications for a user."""
    try:
        return lia_notifications_collection.count_documents({
            "user_id": user_id,
            "is_read": False
        })

    except Exception as e:
        print(f"[Lia] Unread count error: {e}")
        return 0


def mark_read(notification_id: str, user_id: str) -> bool:
    """Mark a single notification as read."""
    try:
        res = lia_notifications_collection.update_one(
            {"id": notification_id, "user_id": user_id},
            {"$set": {"is_read": True}}
        )
        return res.modified_count > 0 or res.matched_count > 0

    except Exception as e:
        print(f"[Lia] Mark read error: {e}")
        return False


def mark_all_read(user_id: str) -> int:
    """Mark all notifications as read. Returns count updated."""
    try:
        res = lia_notifications_collection.update_many(
            {"user_id": user_id, "is_read": False},
            {"$set": {"is_read": True}}
        )
        return res.modified_count

    except Exception as e:
        print(f"[Lia] Mark all read error: {e}")
        return 0


def has_recent_notification(
    user_id: str,
    trigger: str,
    hours: int = 24,
) -> bool:
    """
    Anti-spam check: returns True if the same trigger was sent
    to this user within the last N hours.
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        doc = lia_notifications_collection.find_one({
            "user_id": user_id,
            "trigger": trigger,
            "created_at": {"$gte": cutoff}
        })
        return doc is not None

    except Exception as e:
        print(f"[Lia] Recent check error: {e}")
        return True  # fail-safe: assume sent → don't spam

