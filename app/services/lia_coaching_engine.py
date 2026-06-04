# app/services/lia_coaching_engine.py
"""
Lia Coaching Engine — the brain of the notification system.

Each trigger function:
  1. Queries user data from MongoDB
  2. Checks if condition is met
  3. Anti-spam check (skip if same trigger sent recently)
  4. Calls Claude to generate a personalized 1-2 sentence message
  5. Saves notification to MongoDB
  6. Sends FCM push to user's device(s)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from app.clients.claude_client import ClaudeClient
from app.services.lia_notification_service import (
    save_notification,
    has_recent_notification,
)
from app.services.onesignal import send_push_to_user_subscriptions

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE MESSAGE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

COACHING_SYSTEM = """\
You are Lia, a warm and knowledgeable skincare coach inside the SkinSense app.
Generate a SHORT push notification. Return ONLY a JSON object with "title" and "message" keys.

Rules:
- title: max 50 characters, warm and specific
- message: max 200 characters, personalized to the user's actual data
- Reference specific conditions, scores, products, or routine steps when available
- Be motivational but data-driven — no generic "You got this!" without context
- Educational — briefly explain WHY something matters
- Never alarmist, always encouraging
- Never mention subscriptions or purchases
- No emoji in title, max 1 emoji in message

Example output:
{"title": "Your skin is improving", "message": "Redness dropped 12% since last week. Your niacinamide routine is working — keep it up tonight! 🌙"}
"""


def _generate_coaching_message(
    trigger: str,
    user_context: str,
) -> tuple[str, str]:
    """
    Call Claude to generate a personalized coaching message.
    Returns (title, message) tuple.
    """
    try:
        client = ClaudeClient()

        prompt = (
            f"Trigger type: {trigger}\n\n"
            f"User context:\n{user_context}\n\n"
            "Generate a push notification for this user. "
            "Return ONLY the JSON object with 'title' and 'message'."
        )

        raw = client.text(
            system=COACHING_SYSTEM,
            user=prompt,
            max_tokens=256,
        )

        # Parse JSON from response
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                l for l in cleaned.splitlines()
                if not l.strip().startswith("```")
            ).strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(cleaned[start:end + 1])
            return (
                data.get("title", "Lia has a tip for you"),
                data.get("message", "Check your skincare routine today."),
            )

    except Exception as exc:
        logger.error("Lia coaching message generation failed: %s", exc)

    # Fallback
    return "Lia has a tip for you", "Check your skincare routine today."


def _deliver_notification(
    user_id: str,
    user_doc: dict,
    trigger: str,
    title: str,
    message: str,
    data: dict = None,
):
    """Save to MongoDB + send FCM push."""
    # Save to database
    save_notification(
        user_id=user_id,
        trigger=trigger,
        title=title,
        message=message,
        data=data,
    )

    # Send push notification
    sub_ids = user_doc.get("onesignal_subscriptions", [])
    if sub_ids:
        success, failed = send_push_to_user_subscriptions(
            subscription_ids=sub_ids,
            title=title,
            body=message,
            data={"trigger": trigger, **(data or {})},
        )

        # Clean up invalid subscriptions
        if failed:
            remaining = [
                t for t in sub_ids if t not in failed
            ]
            from app.core.database import users_col
            from bson import ObjectId
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                loop.create_task(
                    users_col().update_one(
                        {"_id": ObjectId(user_id)},
                        {"$set": {"onesignal_subscriptions": remaining}},
                    )
                )
            except Exception:
                pass

        logger.info(
            "[Lia] Push sent to %s: %d/%d devices via OneSignal",
            user_id[:8], success, len(sub_ids),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: POST-SCAN ALERT
#  Called immediately after a face scan with Moderate/Severe conditions.
# ══════════════════════════════════════════════════════════════════════════════

def trigger_post_scan_alert(
    user_id: str,
    user_doc: dict,
    scan_data: dict,
):
    """
    Triggered after a face scan.
    Sends alert if any detected condition is Moderate or Severe.
    """
    if has_recent_notification(user_id, "post_scan_alert", hours=12):
        return

    conditions = scan_data.get("detected_condition", [])
    severe = [
        c for c in conditions
        if c.get("severity") in ("Moderate", "Severe")
    ]

    if not severe:
        return

    context = (
        f"User just completed a face scan.\n"
        f"Overall score: {scan_data.get('overall_score', '?')}/100\n"
        f"Hydration: {scan_data.get('hydration', '?')}%\n"
        f"Detected conditions:\n"
    )
    for c in conditions:
        context += f"  - {c['name']} ({c['severity']}): {c.get('note', '')}\n"

    # Add user profile context
    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "post_scan_alert", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="post_scan_alert",
        title=title,
        message=message,
        data={"source": "face_scan"},
    )

    logger.info("[Lia] Post-scan alert sent to %s", user_id[:8])


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: MORNING ROUTINE REMINDER
# ══════════════════════════════════════════════════════════════════════════════

def trigger_morning_routine(
    user_id: str,
    user_doc: dict,
    routine_steps: list[dict],
    last_scan: dict | None,
):
    """Send morning routine reminder if user has morning steps."""
    if has_recent_notification(user_id, "morning_routine", hours=24):
        return

    if not routine_steps:
        return

    products = [s.get("product_name", "") for s in routine_steps]

    context = (
        f"User has a morning routine with {len(routine_steps)} steps:\n"
        f"Products: {', '.join(products)}\n"
    )

    if last_scan:
        analysis = last_scan.get("analysis", {})
        context += (
            f"Last scan score: {analysis.get('overall_score', '?')}/100\n"
            f"Hydration: {analysis.get('hydration', '?')}%\n"
        )
        conditions = analysis.get("detected_condition", [])
        if conditions:
            context += "Conditions: " + ", ".join(
                f"{c['name']} ({c['severity']})" for c in conditions
            ) + "\n"

    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "morning_routine", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="morning_routine",
        title=title,
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: EVENING ROUTINE REMINDER
# ══════════════════════════════════════════════════════════════════════════════

def trigger_evening_routine(
    user_id: str,
    user_doc: dict,
    routine_steps: list[dict],
    last_scan: dict | None,
):
    """Send evening routine reminder if user has evening/night steps."""
    if has_recent_notification(user_id, "evening_routine", hours=24):
        return

    if not routine_steps:
        return

    products = [s.get("product_name", "") for s in routine_steps]

    context = (
        f"User has an evening routine with {len(routine_steps)} steps:\n"
        f"Products: {', '.join(products)}\n"
    )

    if last_scan:
        analysis = last_scan.get("analysis", {})
        context += (
            f"Last scan score: {analysis.get('overall_score', '?')}/100\n"
        )

    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "evening_routine", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="evening_routine",
        title=title,
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: WEEKLY PROGRESS CHECK-IN
# ══════════════════════════════════════════════════════════════════════════════

def trigger_weekly_progress(
    user_id: str,
    user_doc: dict,
    scans: list[dict],
):
    """
    Weekly check-in comparing last 2 scans.
    Only fires if user has 2+ scans.
    """
    if has_recent_notification(user_id, "weekly_progress", hours=168):
        return  # 7 days

    if len(scans) < 2:
        return

    latest = scans[0].get("analysis", {})
    previous = scans[1].get("analysis", {})

    latest_score = latest.get("overall_score", 0)
    prev_score = previous.get("overall_score", 0)
    score_delta = latest_score - prev_score

    latest_hydration = latest.get("hydration", 0)
    prev_hydration = previous.get("hydration", 0)
    hydration_delta = latest_hydration - prev_hydration

    context = (
        f"Weekly progress comparison:\n"
        f"Latest score: {latest_score}/100 (was {prev_score}, "
        f"{'improved' if score_delta > 0 else 'declined'} by {abs(score_delta)})\n"
        f"Hydration: {latest_hydration}% (was {prev_hydration}%, "
        f"{'up' if hydration_delta > 0 else 'down'} by {abs(hydration_delta)}%)\n"
    )

    # Compare conditions
    latest_conditions = {
        c["name"]: c["severity"]
        for c in latest.get("detected_condition", [])
    }
    prev_conditions = {
        c["name"]: c["severity"]
        for c in previous.get("detected_condition", [])
    }

    improved = []
    worsened = []
    severity_rank = {"Mild": 1, "Moderate": 2, "Severe": 3}

    for name in set(list(latest_conditions) + list(prev_conditions)):
        curr = severity_rank.get(latest_conditions.get(name), 0)
        prev = severity_rank.get(prev_conditions.get(name), 0)
        if curr < prev:
            improved.append(name)
        elif curr > prev:
            worsened.append(name)

    if improved:
        context += f"Improved: {', '.join(improved)}\n"
    if worsened:
        context += f"Worsened: {', '.join(worsened)}\n"

    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "weekly_progress", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="weekly_progress",
        title=title,
        message=message,
        data={
            "score_delta": str(score_delta),
            "hydration_delta": str(hydration_delta),
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: INACTIVITY NUDGE
# ══════════════════════════════════════════════════════════════════════════════

def trigger_inactivity_nudge(
    user_id: str,
    user_doc: dict,
    last_scan_date: datetime | None,
):
    """Nudge if user hasn't scanned in 7+ days."""
    if has_recent_notification(user_id, "inactivity_nudge", hours=72):
        return  # 3 days cooldown

    if last_scan_date is None:
        # New user with no scans — don't nag, welcome message handles this
        return

    days_since = (datetime.now(timezone.utc) - last_scan_date).days

    if days_since < 7:
        return

    context = (
        f"User hasn't done a face scan in {days_since} days.\n"
    )
    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "inactivity_nudge", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="inactivity_nudge",
        title=title,
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: HYDRATION REMINDER
# ══════════════════════════════════════════════════════════════════════════════

def trigger_hydration_reminder(
    user_id: str,
    user_doc: dict,
    last_scan: dict | None,
):
    """Remind if last scan showed low hydration (<40%)."""
    if has_recent_notification(user_id, "hydration_reminder", hours=24):
        return

    if not last_scan:
        return

    analysis = last_scan.get("analysis", {})
    hydration = analysis.get("hydration", 100)

    if hydration >= 40:
        return

    context = (
        f"User's skin hydration is low at {hydration}%.\n"
        f"Hydration target: {analysis.get('hydration_target', '2000')}ml\n"
        f"Overall score: {analysis.get('overall_score', '?')}/100\n"
    )
    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "hydration_reminder", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="hydration_reminder",
        title=title,
        message=message,
        data={"hydration": str(hydration)},
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: STREAK CELEBRATION
# ══════════════════════════════════════════════════════════════════════════════

def trigger_streak_celebration(
    user_id: str,
    user_doc: dict,
    scan_count_last_30_days: int,
):
    """Celebrate consistency if user has scanned 7+ times in 30 days."""
    if has_recent_notification(user_id, "streak_celebration", hours=168):
        return  # 7 days

    if scan_count_last_30_days < 7:
        return

    context = (
        f"User has done {scan_count_last_30_days} scans in the last 30 days. "
        f"They are being very consistent!\n"
    )
    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "streak_celebration", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="streak_celebration",
        title=title,
        message=message,
    )


# ==========================================================================
#  TRIGGER: STRESS CHECK-IN
# ==========================================================================

def trigger_stress_check_in(
    user_id: str,
    user_doc: dict,
):
    """Send a weekly wellness check-in message."""
    if has_recent_notification(user_id, "stress_check_in", hours=168):
        return

    context = (
        "Weekly wellness check-in. Ask the user to notice stress, sleep, "
        "hydration, and routine consistency because those can affect skin.\n"
    )
    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(
        "stress_check_in", context
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="stress_check_in",
        title=title,
        message=message,
        data={"source": "admin_notification_settings"},
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER: NEW USER WELCOME
# ══════════════════════════════════════════════════════════════════════════════

def trigger_new_user_welcome(
    user_id: str,
    user_doc: dict,
):
    """Welcome message after onboarding. Sent only once."""
    if has_recent_notification(user_id, "new_user_welcome", hours=8760):
        return  # 365 days — effectively once

    name = user_doc.get("full_name", "").split()[0] if user_doc.get("full_name") else ""
    greeting = f"Hi {name}! " if name else ""

    title = "Welcome to SkinSense!"
    message = (
        f"{greeting}I'm Lia, your personal skincare coach. "
        "Do a quick face scan and I'll build your perfect routine."
    )

    _deliver_notification(
        user_id=user_id,
        user_doc=user_doc,
        trigger="new_user_welcome",
        title=title,
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ON-DEMAND COACHING
# ══════════════════════════════════════════════════════════════════════════════

def generate_on_demand_coaching(
    user_doc: dict,
    last_scan: dict | None,
    routine_steps: list[dict],
    context_type: str = "general",
) -> dict:
    """
    Generate a coaching message on demand (for POST /lia/coaching).
    Returns {"title": "...", "message": "..."}.
    """
    context = f"Context type: {context_type}\n"

    if last_scan:
        analysis = last_scan.get("analysis", {})
        context += (
            f"Last scan score: {analysis.get('overall_score', '?')}/100\n"
            f"Hydration: {analysis.get('hydration', '?')}%\n"
        )
        conditions = analysis.get("detected_condition", [])
        if conditions:
            context += "Conditions: " + ", ".join(
                f"{c['name']} ({c['severity']})" for c in conditions
            ) + "\n"

    if routine_steps:
        products = [s.get("product_name", "") for s in routine_steps]
        context += f"Routine products: {', '.join(products)}\n"
    else:
        context += "User has no routine steps yet.\n"

    context += _format_profile_context(user_doc)

    title, message = _generate_coaching_message(context_type, context)

    return {"title": title, "message": message}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _format_profile_context(user_doc: dict) -> str:
    """Format user profile as compact context for Claude."""
    parts = []

    name = user_doc.get("full_name")
    if name:
        parts.append(f"Name: {name.split()[0]}")

    skin_type = user_doc.get("skin_type")
    if skin_type:
        parts.append(f"Skin type: {skin_type}")

    concerns = user_doc.get("skin_concerns", [])
    if concerns:
        parts.append(f"Skin concerns: {', '.join(concerns)}")

    hair_type = user_doc.get("hair_type")
    if hair_type:
        parts.append(f"Hair type: {hair_type}")

    phase = user_doc.get("current_phase")
    if phase:
        parts.append(f"Phase: {phase.replace('_', ' ')}")

    if parts:
        return "User profile: " + " | ".join(parts) + "\n"
    return ""
