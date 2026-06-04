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
from app.core.mongo_client import saved_routines_collection

from app.services.lia_coaching_engine import (
    trigger_morning_routine,
    trigger_evening_routine,
    trigger_weekly_progress,
    trigger_stress_check_in,
    trigger_inactivity_nudge,
    trigger_hydration_reminder,
    trigger_streak_celebration,
)
from app.services.admin_notification_settings import get_notification_settings

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
            "life_phase": 1,
            "pregnancy_start_month": 1,
            "pregnancy_month_updated_at": 1,
            "allergies": 1,
            "onesignal_subscriptions": 1,
        },
    )
    users = await cursor.to_list(length=5000)
    from app.utils.pregnancy_utils import resolve_pregnancy_phase
    for u in users:
        resolve_pregnancy_phase(u)
    return users


async def _get_user_routine_steps(user_id: str, time_filter: str) -> list[dict]:
    """Fetch routine steps from MongoDB for a specific time (morning/night)."""
    try:
        cursor = await asyncio.to_thread(
            lambda: list(saved_routines_collection.find(
                {"user_id": user_id, "time": time_filter},
                {"_id": 0}
            ))
        )
        return cursor
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


async def run_stress_check_in():
    """Weekly wellness check-in."""
    logger.info("[Lia Scheduler] Running stress check-ins...")

    users = await _get_all_active_users()

    for user in users:
        try:
            user_id = str(user["_id"])
            await asyncio.get_event_loop().run_in_executor(
                None,
                trigger_stress_check_in,
                user_id, user,
            )

        except Exception as exc:
            logger.warning(
                "[Lia] Stress check-in failed for %s: %s",
                str(user["_id"])[:8], exc,
            )

    logger.info("[Lia Scheduler] Stress check-in job complete.")


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


async def run_daily_routine_reset():
    """Midnight — reset daily routine completion state in MongoDB."""
    logger.info("[Lia Scheduler] Resetting daily routine completion state...")

    def _reset():
        saved_routines_collection.update_many(
            {"time": {"$in": ["morning", "night"]}},
            {"$set": {"is_completed": False, "completed_at": None}},
        )

    await asyncio.to_thread(_reset)
    logger.info("[Lia Scheduler] Daily routine reset complete.")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return 8, 0


def _schedule_reminder_job(scheduler: AsyncIOScheduler, reminder: dict):
    if not reminder.get("enabled", False):
        return

    runners = {
        "morning_routine": run_morning_routine,
        "night_routine": run_evening_routine,
        "weekly_scan_reminder": run_weekly_progress,
        "stress_check_in": run_stress_check_in,
    }
    runner = runners.get(reminder.get("id"))
    if not runner:
        logger.warning(
            "[Lia Scheduler] No runner mapped for reminder %s",
            reminder.get("id"),
        )
        return

    schedule = reminder.get("schedule") or {}
    hour, minute = _parse_hhmm(schedule.get("time", "08:00"))
    cron_args = {"hour": hour, "minute": minute}
    if schedule.get("type") == "weekly":
        cron_args["day_of_week"] = schedule.get("day_of_week") or "sun"

    scheduler.add_job(
        runner,
        CronTrigger(**cron_args),
        id=f"lia_{reminder['id']}",
        name=f"Lia {reminder.get('title', reminder['id'])}",
        replace_existing=True,
    )


def start_scheduler():
    """Start the Lia notification scheduler."""
    global _scheduler

    if _scheduler:
        stop_scheduler()

    _scheduler = AsyncIOScheduler()
    settings = get_notification_settings()
    for reminder in settings["reminders"]:
        _schedule_reminder_job(_scheduler, reminder)

    # Periodic checks — every 6 hours
    _scheduler.add_job(
        run_periodic_checks,
        IntervalTrigger(hours=6),
        id="lia_periodic",
        name="Lia Periodic Checks",
        replace_existing=True,
    )

    # Daily routine completion reset - midnight
    _scheduler.add_job(
        run_daily_routine_reset,
        CronTrigger(hour=0, minute=0),
        id="routine_daily_reset",
        name="Daily Routine Reset (midnight)",
        replace_existing=True,
    )

    _scheduler.start()
    job_count = len(_scheduler.get_jobs())
    logger.info(
        "[Lia Scheduler] Started with %d jobs from admin notification settings.",
        job_count,
    )


def stop_scheduler():
    """Stop the scheduler gracefully."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("[Lia Scheduler] Stopped.")
        _scheduler = None
