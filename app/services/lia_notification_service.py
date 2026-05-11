# app/services/lia_notification_service.py
"""
CRUD operations for the lia_notifications Supabase table.
Handles saving, fetching, reading, and anti-spam checks.
"""

from datetime import datetime, timezone, timedelta
from app.core.supabase_client import supabase


def save_notification(
    user_id: str,
    trigger: str,
    title: str,
    message: str,
    data: dict = None,
) -> dict | None:
    """Insert a notification into Supabase. Returns the inserted row."""
    try:
        response = supabase.table("lia_notifications").insert({
            "user_id": user_id,
            "trigger": trigger,
            "title": title,
            "message": message,
            "data": data or {},
            "is_read": False,
        }).execute()

        return response.data[0] if response.data else None

    except Exception as e:
        print(f"[Lia] Notification save error: {e}")
        return None


def get_user_notifications(
    user_id: str,
    limit: int = 20,
    unread_only: bool = False,
) -> list[dict]:
    """Fetch notifications for a user, newest first."""
    try:
        query = supabase.table("lia_notifications") \
            .select("*") \
            .eq("user_id", user_id)

        if unread_only:
            query = query.eq("is_read", False)

        response = query \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()

        return response.data or []

    except Exception as e:
        print(f"[Lia] Notification fetch error: {e}")
        return []


def get_unread_count(user_id: str) -> int:
    """Count unread notifications for a user."""
    try:
        response = supabase.table("lia_notifications") \
            .select("id", count="exact") \
            .eq("user_id", user_id) \
            .eq("is_read", False) \
            .execute()

        return response.count or 0

    except Exception as e:
        print(f"[Lia] Unread count error: {e}")
        return 0


def mark_read(notification_id: str, user_id: str) -> bool:
    """Mark a single notification as read."""
    try:
        response = supabase.table("lia_notifications") \
            .update({"is_read": True}) \
            .eq("id", notification_id) \
            .eq("user_id", user_id) \
            .execute()

        return bool(response.data)

    except Exception as e:
        print(f"[Lia] Mark read error: {e}")
        return False


def mark_all_read(user_id: str) -> int:
    """Mark all notifications as read. Returns count updated."""
    try:
        response = supabase.table("lia_notifications") \
            .update({"is_read": True}) \
            .eq("user_id", user_id) \
            .eq("is_read", False) \
            .execute()

        return len(response.data) if response.data else 0

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

        response = supabase.table("lia_notifications") \
            .select("id") \
            .eq("user_id", user_id) \
            .eq("trigger", trigger) \
            .gte("created_at", cutoff) \
            .limit(1) \
            .execute()

        return bool(response.data)

    except Exception as e:
        print(f"[Lia] Recent check error: {e}")
        return True  # fail-safe: assume sent → don't spam
