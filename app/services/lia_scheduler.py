# app/services/lia_scheduler.py
"""
Lia Scheduler — APScheduler cron jobs for proactive coaching notifications.

Schedule overview:
  8:00 AM   → morning routine reminders
  9:00 PM   → evening routine reminders
  Sunday 10 AM → weekly progress check-ins
  Every 6h  → inactivity nudge + hydration reminder + streak celebration
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.database import get_db
from app.core.supabase_client import supabase

from app.services.lia_coaching_engine import (
    trigger_morning_routine,
    trigger_evening_routine,
    trigger_weekly_progress,
    trigger_inactivity_nudge,
    trigger_hydration_reminder,
    trigger_streak_celebration,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  USER DATA HELPERS (async → sync bridge for trigger functions)
# ══════════════════════════════════════════════════════════════════════════════

async def _get_all_active_users() -> list[dict]:
    """Fetch all active users with completed onboarding."""
    db = get_db()
    cursor = db.users.find(
        {
            "is_active": True,
            "onboarding_completed": True,
        },
        {
            "_id": 1,
            "full_name": 1,
            "skin_type": 1,
            "skin_concerns": 1,
            "hair_type": 1,
            "hair_concerns": 1,
            "current_phase": 1,
            "allergies": 1,
            "fcm_tokens": 1,
        },
    )
    return await cursor.to_list(length=5000)


async def _get_user_routine_steps(user_id: str, time_filter: str) -> list[dict]:
    """Fetch routine steps from Supabase for a specific time (morning/night)."""
    try:
        response = supabase.table("saved_routines") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("time", time_filter) \
            .execute()
        return response.data or []
    except Exception:
        return []


async def _get_user_scans(user_id: str, limit: int = 2) -> list[dict]:
    """Fetch recent face scans from MongoDB."""
    db = get_db()
    cursor = db.face_scans.find(
        {"user_id": user_id},
    ).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def _get_scan_count_last_30_days(user_id: str) -> int:
    """Count scans in the last 30 days."""
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return await db.face_scans.count_documents({
        "user_id": user_id,
        "created_at": {"$gte": cutoff},
    })


# ══════════════════════════════════════════════════════════════════════════════
#  CRON JOB RUNNERS
# ══════════════════════════════════════════════════════════════════════════════

async def run_morning_routine():
    """8:00 AM — Send morning routine reminders."""
    logger.info("[Lia Scheduler] Running morning routine reminders...")

    users = await _get_all_active_users()

    for user in users:
        try:
            user_id = str(user["_id"])
            steps = await _get_user_routine_steps(user_id, "morning")

            if not steps:
                continue

            scans = await _get_user_scans(user_id, limit=1)
            last_scan = scans[0] if scans else None

            # Run in executor to avoid blocking async loop
            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_morning_routine,
                user_id, user, steps, last_scan,
            )

        except Exception as exc:
            logger.warning(
                "[Lia] Morning trigger failed for %s: %s",
                str(user["_id"])[:8], exc,
            )

    logger.info("[Lia Scheduler] Morning routine job complete.")


async def run_evening_routine():
    """9:00 PM — Send evening routine reminders."""
    logger.info("[Lia Scheduler] Running evening routine reminders...")

    users = await _get_all_active_users()

    for user in users:
        try:
            user_id = str(user["_id"])
            steps = await _get_user_routine_steps(user_id, "night")

            if not steps:
                continue

            scans = await _get_user_scans(user_id, limit=1)
            last_scan = scans[0] if scans else None

            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_evening_routine,
                user_id, user, steps, last_scan,
            )

        except Exception as exc:
            logger.warning(
                "[Lia] Evening trigger failed for %s: %s",
                str(user["_id"])[:8], exc,
            )

    logger.info("[Lia Scheduler] Evening routine job complete.")


async def run_weekly_progress():
    """Sunday 10:00 AM — Weekly progress check-in."""
    logger.info("[Lia Scheduler] Running weekly progress check-ins...")

    users = await _get_all_active_users()

    for user in users:
        try:
            user_id = str(user["_id"])
            scans = await _get_user_scans(user_id, limit=2)

            if len(scans) < 2:
                continue

            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_weekly_progress,
                user_id, user, scans,
            )

        except Exception as exc:
            logger.warning(
                "[Lia] Weekly trigger failed for %s: %s",
                str(user["_id"])[:8], exc,
            )

    logger.info("[Lia Scheduler] Weekly progress job complete.")


async def run_periodic_checks():
    """Every 6 hours — inactivity, hydration, streaks."""
    logger.info("[Lia Scheduler] Running periodic checks...")

    users = await _get_all_active_users()

    for user in users:
        try:
            user_id = str(user["_id"])
            scans = await _get_user_scans(user_id, limit=1)
            last_scan = scans[0] if scans else None

            # ── Inactivity nudge ─────────────────────────────────
            last_scan_date = None
            if last_scan:
                last_scan_date = last_scan.get("created_at")
                if last_scan_date and not last_scan_date.tzinfo:
                    last_scan_date = last_scan_date.replace(
                        tzinfo=timezone.utc
                    )

            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_inactivity_nudge,
                user_id, user, last_scan_date,
            )

            # ── Hydration reminder ───────────────────────────────
            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_hydration_reminder,
                user_id, user, last_scan,
            )

            # ── Streak celebration ───────────────────────────────
            scan_count = await _get_scan_count_last_30_days(user_id)

            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_streak_celebration,
                user_id, user, scan_count,
            )

        except Exception as exc:
            logger.warning(
                "[Lia] Periodic check failed for %s: %s",
                str(user["_id"])[:8], exc,
            )

    logger.info("[Lia Scheduler] Periodic checks complete.")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler():
    """Start the Lia notification scheduler."""
    global _scheduler

    _scheduler = AsyncIOScheduler()

    # Morning routine — 8:00 AM
    _scheduler.add_job(
        run_morning_routine,
        CronTrigger(hour=8, minute=0),
        id="lia_morning",
        name="Lia Morning Routine",
        replace_existing=True,
    )

    # Evening routine — 9:00 PM
    _scheduler.add_job(
        run_evening_routine,
        CronTrigger(hour=21, minute=0),
        id="lia_evening",
        name="Lia Evening Routine",
        replace_existing=True,
    )

    # Weekly progress — Sunday 10:00 AM
    _scheduler.add_job(
        run_weekly_progress,
        CronTrigger(day_of_week="sun", hour=10, minute=0),
        id="lia_weekly",
        name="Lia Weekly Progress",
        replace_existing=True,
    )

    # Periodic checks — every 6 hours
    _scheduler.add_job(
        run_periodic_checks,
        IntervalTrigger(hours=6),
        id="lia_periodic",
        name="Lia Periodic Checks",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "[Lia Scheduler] Started with 4 jobs: "
        "morning(8AM), evening(9PM), weekly(Sun 10AM), periodic(6h)"
    )


def stop_scheduler():
    """Stop the scheduler gracefully."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("[Lia Scheduler] Stopped.")
        _scheduler = None
