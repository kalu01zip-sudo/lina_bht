# routers/chat.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Gixy AI Coach  (v4 — Personalized Coach)    ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /chat/message      → Send a message (SSE streaming reply)║
║   POST /chat/message/sync → Send a message (full JSON reply)    ║
║   GET  /chat/history      → Get last N messages                 ║
║   DELETE /chat/history    → Clear all chat history              ║
╚══════════════════════════════════════════════════════════════════╝

Gixy is a warm, motivational, personalized skincare coach that:
  • Knows the user's full profile, skin/hair concerns, and allergies
  • References actual scan results and progress over time
  • Encourages consistency and educates users on WHY things work
  • Never diagnoses, never pushes subscriptions

All user data (profile, scans, routines, memories, subscription,
profile score) is fetched in parallel on every message via
asyncio.gather for minimal latency.

SSE streaming format:
  data: {"chunk": "Hello"}
  data: {"chunk": " there"}
  data: [DONE]

Auth: Bearer JWT — same token your other routes use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import AsyncGenerator, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.core.database import get_db
from app.clients.claude_client import async_stream_chat, ClaudeClient, USE_LOCAL_LLM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Gixy Chat"])

# ── Memory config ─────────────────────────────────────────────────────────────
# Summariser fires every N user messages; keeps newest MAX_MEMORY_BULLETS bullets.
MEMORY_TRIGGER_EVERY = 10
MAX_MEMORY_BULLETS   = 200   # raised from 60 in v2


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════

from jose import JWTError, jwt as jose_jwt

_bearer = HTTPBearer()


def _get_current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    secret = os.getenv("SECRET_KEY", "")
    token  = creds.credentials
    try:
        payload = jose_jwt.decode(token, secret, algorithms=["HS256"])
        uid = payload.get("sub") or payload.get("user_id") or payload.get("id")
        if not uid:
            raise ValueError("No user identifier in token")
        return str(uid)
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  REQUEST / RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class ChatMessageRequest(BaseModel):
    message: str
    model_config = {
        "json_schema_extra": {
            "example": {"message": "What ingredients should I avoid for oily skin?"}
        }
    }


class ChatMessage(BaseModel):
    role:       str
    content:    str
    created_at: datetime


class ChatHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    messages: list[ChatMessage]


class ChatSyncResponse(BaseModel):
    """Returned by POST /chat/message/sync -- the complete reply in one JSON response."""
    reply: str


# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADERS
#  All loaders are user-scoped: every query filters by user_id.
#  No loader can return data belonging to a different user.
# ══════════════════════════════════════════════════════════════════════════════

async def _get_user_profile(user_id: str) -> dict | None:
    """Fetch the full user document from MongoDB."""
    db = get_db()
    try:
        doc = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        doc = await db.users.find_one({"_id": user_id})
    return doc


async def _get_latest_scans(user_id: str) -> tuple[dict | None, dict | None]:
    """
    Fetch the most recent face scan and the most recent hair/scalp scan.
    Face scans are stored in `face_scans` collection (via scan_collection).
    Hair/scalp scans are stored in `scalp_scans` collection (if exists).
    """
    db      = get_db()
    uid_str = str(user_id)

    face_doc, scalp_doc = await asyncio.gather(
        db.face_scans.find_one(
            {"user_id": uid_str},
            sort=[("created_at", -1)],
        ),
        db.scalp_scans.find_one(
            {"user_id": uid_str},
            sort=[("created_at", -1)],
        ),
    )
    return face_doc, scalp_doc


async def _get_user_memories(user_id: str) -> list[str]:
    """
    Returns the user's accumulated memory bullets from users.user_memories.
    Returns [] for new users who have no memories yet.
    """
    db = get_db()
    try:
        doc = await db.users.find_one(
            {"_id": ObjectId(user_id)},
            {"user_memories": 1},
        )
    except Exception:
        doc = await db.users.find_one(
            {"_id": user_id},
            {"user_memories": 1},
        )
    if not doc:
        return []
    return doc.get("user_memories") or []


async def _get_routine_steps(user_id: str) -> dict[str, list[dict]]:
    """
    Fetch the user's current routine steps, grouped by time_slot.
    Checks BOTH MongoDB `routine_steps` AND MongoDB `saved_routines`.

    Returns a dict with keys "morning", "night", "weekly".
    Each value is a list of step dicts:
      { product_name, title (optional), bio (optional), instructions (optional) }
    """
    db   = get_db()
    uid_str = str(user_id)

    # Source 1: MongoDB routine_steps
    docs = await (
        db.routine_steps
        .find({"user_id": uid_str})
        .sort("order", 1)
        .to_list(200)
    )

    grouped: dict[str, list[dict]] = {"morning": [], "night": [], "weekly": []}

    for doc in docs:
        slot = doc.get("time_slot", "morning")
        if slot not in grouped:
            continue
        grouped[slot].append({
            "product_name":  doc.get("product_name", ""),
            "title":         doc.get("title") or None,
            "bio":           doc.get("bio") or doc.get("instructions") or None,
            "instructions":  doc.get("instructions") or None,
            "is_completed":  doc.get("completed_date") == date.today().isoformat(),
        })

    # Source 2: MongoDB saved_routines (if MongoDB routine_steps was empty)
    has_mongo_steps = any(grouped.get(s) for s in ("morning", "night", "weekly"))

    if not has_mongo_steps:
        try:
            from app.core.mongo_client import saved_routines_collection
            cursor = saved_routines_collection.find({"user_id": uid_str}, {"_id": 0})
            rows = list(cursor)

            for row in rows:
                time_slot = row.get("time", "morning")
                # Map "night" to match grouped keys
                if time_slot not in grouped:
                    if time_slot in ("evening", "night"):
                        time_slot = "night"
                    else:
                        continue

                grouped.setdefault(time_slot, []).append({
                    "product_name":  row.get("product_name") or row.get("name", ""),
                    "title":         row.get("title") or None,
                    "bio":           row.get("description") or row.get("bio") or None,
                    "instructions":  row.get("instructions") or None,
                    "is_completed":  False,
                })
        except Exception as exc:
            logger.warning("MongoDB saved_routines fetch failed: %s", exc)

    return grouped


async def _get_product_scans(user_id: str, limit: int = 3) -> list[dict]:
    """
    Fetch the most recent product scans for this user (newest first).
    Product scans are stored in `product_scan_history` collection.
    """
    db      = get_db()
    uid_str = str(user_id)
    docs    = await (
        db.product_scan_history
        .find({"user_id": uid_str})
        .sort("created_at", -1)
        .limit(limit)
        .to_list(length=limit)
    )
    return docs


def _compute_profile_score(profile: dict | None) -> dict | None:
    """
    Compute profile score metrics from the user's profile document.
    Uses ProfileScorer with client=None (rule-based note — zero API calls).
    Returns None silently on any error so it never breaks the chat flow.
    """
    if not profile:
        return None
    try:
        from app.services.profile_scorer import ProfileScorer, UserProfile  # local import — avoids circular deps
        up = UserProfile(
            current_phase = profile.get("current_phase"),
            allergies     = profile.get("allergies")     or [],
            skin_type     = profile.get("skin_type"),
            skin_concerns = profile.get("skin_concerns") or [],
            hair_type     = profile.get("hair_type"),
            hair_concerns = profile.get("hair_concerns") or [],
        )
        return ProfileScorer(client=None).score(up)
    except Exception as exc:
        logger.warning("Profile score computation failed: %s", exc)
        return None


async def _get_subscription_status(user_id: str) -> str:
    """
    Returns 'premium', 'trialing', or 'free'.
    Queries the subscriptions collection for the most recent active/trialing record.
    """
    db = get_db()
    try:
        sub = await db.subscriptions.find_one(
            {
                "user_id": str(user_id),
                "status":  {"$in": ["active", "trialing"]},
            },
            sort=[("created_at", -1)],
        )
        if not sub:
            return "free"
        return "trialing" if sub.get("status") == "trialing" else "premium"
    except Exception as exc:
        logger.warning("Subscription check failed for user %s: %s", user_id, exc)
        return "free"


async def _get_scan_progress(user_id: str) -> dict | None:
    """
    Compare the two most recent face scans to compute progress.
    Returns a dict with score deltas and improved/worsened conditions,
    or None if fewer than 2 scans exist.
    """
    db = get_db()
    uid_str = str(user_id)

    try:
        scans = await (
            db.face_scans
            .find({"user_id": uid_str})
            .sort("created_at", -1)
            .limit(2)
            .to_list(length=2)
        )
    except Exception:
        scans = []

    if len(scans) < 2:
        return None

    latest = scans[0].get("analysis", {})
    previous = scans[1].get("analysis", {})

    latest_score = latest.get("overall_score", 0)
    prev_score = previous.get("overall_score", 0)
    latest_hydration = latest.get("hydration", 0)
    prev_hydration = previous.get("hydration", 0)

    # Compare condition severities
    severity_rank = {"Mild": 1, "Moderate": 2, "Severe": 3}
    latest_conds = {
        c["name"]: c["severity"]
        for c in latest.get("detected_condition", [])
    }
    prev_conds = {
        c["name"]: c["severity"]
        for c in previous.get("detected_condition", [])
    }

    improved = []
    worsened = []
    for name in set(list(latest_conds) + list(prev_conds)):
        curr = severity_rank.get(latest_conds.get(name), 0)
        prev = severity_rank.get(prev_conds.get(name), 0)
        if curr < prev:
            improved.append(name)
        elif curr > prev:
            worsened.append(name)

    return {
        "latest_score": latest_score,
        "previous_score": prev_score,
        "score_delta": latest_score - prev_score,
        "latest_hydration": latest_hydration,
        "previous_hydration": prev_hydration,
        "improved_conditions": improved,
        "worsened_conditions": worsened,
    }


async def _get_scan_history(
    user_id: str, face_limit: int = 5, scalp_limit: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Fetch recent face + scalp scan summaries for trend context."""
    db = get_db()
    uid_str = str(user_id)

    face_docs, scalp_docs = await asyncio.gather(
        db.face_scans.find(
            {"user_id": uid_str},
            {
                "analysis.overall_score": 1,
                "analysis.hydration": 1,
                "analysis.detected_condition.name": 1,
                "analysis.detected_condition.severity": 1,
                "created_at": 1,
            },
        ).sort("created_at", -1).limit(face_limit).to_list(face_limit),

        db.scalp_scans.find(
            {"user_id": uid_str},
            {
                "analysis.overall_score": 1,
                "analysis.detected_condition.name": 1,
                "analysis.detected_condition.severity": 1,
                "created_at": 1,
            },
        ).sort("created_at", -1).limit(scalp_limit).to_list(scalp_limit),
    )
    return face_docs, scalp_docs


async def _get_scalp_progress(user_id: str) -> dict | None:
    """
    Compare the two most recent scalp scans to compute progress.
    Returns a dict with score deltas and improved/worsened conditions,
    or None if fewer than 2 scalp scans exist.
    """
    db = get_db()
    uid_str = str(user_id)
    try:
        scans = await (
            db.scalp_scans
            .find({"user_id": uid_str})
            .sort("created_at", -1)
            .limit(2)
            .to_list(length=2)
        )
    except Exception:
        scans = []

    if len(scans) < 2:
        return None

    latest = scans[0].get("analysis", {})
    previous = scans[1].get("analysis", {})

    latest_score = latest.get("overall_score", 0)
    prev_score = previous.get("overall_score", 0)

    severity_rank = {"Mild": 1, "Moderate": 2, "Severe": 3}
    latest_conds = {
        c["name"]: c["severity"]
        for c in latest.get("detected_condition", [])
        if isinstance(c, dict)
    }
    prev_conds = {
        c["name"]: c["severity"]
        for c in previous.get("detected_condition", [])
        if isinstance(c, dict)
    }

    improved = []
    worsened = []
    for name in set(list(latest_conds) + list(prev_conds)):
        curr = severity_rank.get(latest_conds.get(name), 0)
        prev = severity_rank.get(prev_conds.get(name), 0)
        if curr < prev:
            improved.append(name)
        elif curr > prev:
            worsened.append(name)

    return {
        "latest_score": latest_score,
        "previous_score": prev_score,
        "score_delta": latest_score - prev_score,
        "improved_conditions": improved,
        "worsened_conditions": worsened,
    }


async def _get_nutrition_recommendations(
    detected_conditions: list[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Fetch nutrition, food, and recipe recommendations based on detected conditions.
    Uses sync pymongo collections via asyncio.to_thread.
    """
    if not detected_conditions:
        return [], [], []

    try:
        from app.core.mongo_client import (
            nutritions_collection,
            foods_collection,
            recipes_collection,
        )

        def _fetch():
            conditions = [
                c.lower().strip().replace(" ", "_") for c in detected_conditions
            ]
            query = {"detected_condition": {"$in": conditions}}

            nutritions = list(
                nutritions_collection.find(
                    query,
                    {"_id": 0, "name": 1, "benefits": 1, "detected_condition": 1},
                ).limit(5)
            )
            foods = list(
                foods_collection.find(
                    query,
                    {
                        "_id": 0, "name": 1, "ingredients": 1,
                        "benefits": 1, "detected_condition": 1,
                    },
                ).limit(5)
            )
            recipes = list(
                recipes_collection.find(
                    query,
                    {
                        "_id": 0, "name": 1, "main_ingredients": 1,
                        "how_it_improves": 1, "detected_condition": 1,
                    },
                ).limit(3)
            )
            return nutritions, foods, recipes

        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("Nutrition recommendations fetch failed: %s", exc)
        return [], [], []


async def _get_latest_cached_routine_check(user_id: str) -> dict | None:
    """
    Fetch the most recently stored routine-check result from
    `db.routine_check_cache` for this user, or None if no cache entry exists.
    """
    db = get_db()
    try:
        doc = await db.routine_check_cache.find_one(
            {"user_id": str(user_id)},
            sort=[("cached_at", -1)],
        )
        return doc
    except Exception as exc:
        logger.warning("Routine check cache fetch failed for user %s: %s", user_id, exc)
        return None


async def _load_history(user_id: str, limit: int = 20) -> list[dict]:
    """Load last N chat messages as role/content dicts for LLM context."""
    db = get_db()
    cursor = (
        db.chat_messages
        .find({"user_id": user_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    docs.reverse()
    return [{"role": d["role"], "content": d["content"]} for d in docs]


async def _save_message(user_id: str, role: str, content: str) -> None:
    db = get_db()
    await db.chat_messages.insert_one({
        "user_id":    user_id,
        "role":       role,
        "content":    content,
        "created_at": datetime.now(timezone.utc),
    })


async def _count_user_messages(user_id: str) -> int:
    db = get_db()
    return await db.chat_messages.count_documents(
        {"user_id": user_id, "role": "user"}
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CONTEXT FORMATTERS
#  Each formatter converts raw DB data into a compact, readable text block
#  ready for system prompt injection.  Returns "" when there is no data.
# ══════════════════════════════════════════════════════════════════════════════

def _format_scan_context(face: dict | None, scalp: dict | None) -> str:
    """
    Format the latest face scan + hair/scalp scan into a readable block.
    Face scan data is stored as: { analysis: { overall_score, hydration, detected_condition, ... } }
    """
    if not face and not scalp:
        return ""

    lines: list[str] = [
        "── LATEST SCAN RESULTS ──────────────────────────────────────────",
    ]

    def _fmt_face_scan(doc: dict) -> list[str]:
        out: list[str] = []
        # The AI analysis data is nested under "analysis"
        analysis = doc.get("analysis", doc)

        # Date
        created = doc.get("created_at")
        date_str = (
            created.strftime("%d %b %Y")
            if isinstance(created, datetime)
            else str(created)[:10] if created else "unknown"
        )

        score = analysis.get("overall_score", "?")
        hydration = analysis.get("hydration", "?")
        skin_type = analysis.get("skin_type", "?")

        out.append(f"  Face scan  (date: {date_str})")
        out.append(f"  Overall score: {score}/100")
        out.append(f"  Hydration: {hydration}%")
        out.append(f"  Skin type: {skin_type}")

        # Summary
        summary = analysis.get("summary")
        if summary:
            out.append(f"  Summary: {summary}")

        # Detected conditions
        conditions = analysis.get("detected_condition", [])
        if conditions:
            out.append("  Detected conditions:")
            for c in conditions:
                name = c.get("name", "Unknown")
                severity = c.get("severity", "?")
                note = c.get("note", "")
                out.append(f"    - {name} [{severity}]: {note}")

        # Recommendations
        recs = analysis.get("recommendations", [])
        if recs:
            out.append("  AI recommendations:")
            for r in recs[:5]:  # limit to 5
                out.append(f"    - {r}")

        return out

    def _fmt_scalp_scan(doc: dict) -> list[str]:
        out: list[str] = []
        analysis = doc.get("analysis", doc)

        created = doc.get("created_at")
        date_str = (
            created.strftime("%d %b %Y")
            if isinstance(created, datetime)
            else str(created)[:10] if created else "unknown"
        )

        out.append(f"  Hair/Scalp scan  (date: {date_str})")

        for key in ("overall_score", "scalp_health", "hair_condition"):
            val = analysis.get(key)
            if val is not None:
                out.append(f"  {key.replace('_', ' ').title()}: {val}")

        conditions = analysis.get("detected_condition", [])
        if conditions:
            out.append("  Detected conditions:")
            for c in conditions:
                name = c.get("name", "Unknown")
                severity = c.get("severity", "?")
                out.append(f"    - {name} [{severity}]")

        return out

    if face:
        lines.extend(_fmt_face_scan(face))
    if scalp:
        if face:
            lines.append("")
        lines.extend(_fmt_scalp_scan(scalp))

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_routine_context(routine: dict[str, list[dict]]) -> str:
    """
    Format the user's current routine steps into a compact text block.
    Empty slots are silently omitted.  Returns "" if the routine is empty.
    """
    has_steps = any(routine.get(slot) for slot in ("morning", "night", "weekly"))
    if not has_steps:
        return ""

    lines: list[str] = [
        "── CURRENT ROUTINE ──────────────────────────────────────────────",
    ]
    for slot in ("morning", "night", "weekly"):
        steps = routine.get(slot, [])
        if not steps:
            continue
        lines.append(f"  {slot.capitalize()}:")
        for s in steps:
            name  = s["product_name"]
            title = s.get("title")
            bio   = s.get("bio")
            # Show title in parentheses only when it differs from the product name
            label = f"{title} ({name})" if (title and title.lower() != name.lower()) else name
            suffix = f" — {bio}" if bio else ""
            lines.append(f"    • {label}{suffix}")
    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_product_scans_context(scans: list[dict]) -> str:
    """
    Format the user's three most recent product scans into a compact text block.
    Handles both flat and nested product scan document shapes gracefully.
    """
    if not scans:
        return ""

    lines: list[str] = [
        "── RECENT PRODUCT SCANS (last 3) ───────────────────────────────",
    ]
    for scan in scans:
        created_at = scan.get("created_at", "")
        date_str   = (
            created_at.strftime("%d %b %Y")
            if isinstance(created_at, datetime)
            else str(created_at)[:10] if created_at else "unknown"
        )

        # product name — try flat field first, then nested product sub-doc
        nested  = scan.get("product") or {}
        name    = scan.get("product_name") or nested.get("name",  "Unknown product")
        brand   = scan.get("brand")        or nested.get("brand", "")
        label   = f"{brand} {name}".strip() if brand else name

        # compatibility — try flat field first, then nested analysis sub-doc
        analysis = scan.get("analysis") or {}
        compat   = scan.get("compatibility")       or analysis.get("compatibility",       "")
        c_score  = scan.get("compatibility_score") or analysis.get("compatibility_score")
        summary  = scan.get("summary")             or analysis.get("summary",             "")

        score_str = f", score {c_score}/100" if c_score is not None else ""
        lines.append(f"  • {date_str} — {label} [{compat}{score_str}]")
        if summary:
            lines.append(f"    {summary}")

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_profile_score_context(score_data: dict | None) -> str:
    """
    Format the computed profile score metrics into a compact text block.
    Returns "" if scoring failed or no profile exists.
    """
    if not score_data:
        return ""

    lines: list[str] = [
        "── PROFILE SCORE METRICS ────────────────────────────────────────",
        f"  • Overall care score : {score_data.get('score', '?')}/95",
        f"  • Hydration level    : {score_data.get('hydration', '?')}%",
        f"  • Acne risk          : {score_data.get('acne_risk', '?')}",
        f"  • Skin sensitivity   : {score_data.get('sensitivity', '?')}",
    ]
    note = score_data.get("note", "")
    if note:
        lines.append(f"  • Personalised note  : {note}")
    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_subscription_context(status: str) -> str:
    """
    Format the user's subscription status for the system prompt.
    Gixy uses this to know which premium features to mention.
    """
    labels = {
        "premium":  "Premium (full access — AI Check, all premium features unlocked)",
        "trialing": "Free Trial (premium features active during trial period)",
        "free":     "Free plan (AI Check and other premium features are locked)",
    }
    return (
        "── SUBSCRIPTION STATUS ──────────────────────────────────────────\n"
        f"  {labels.get(status, status)}\n"
        "─────────────────────────────────────────────────────────────────"
    )


def _format_scan_history_context(
    face_history: list[dict], scalp_history: list[dict],
) -> str:
    """
    Format scan history timeline so Gixy can discuss trends.
    Shows the last 5 face scans and last 3 scalp scans in date order.
    """
    if not face_history and not scalp_history:
        return ""

    lines: list[str] = [
        "── SCAN HISTORY (recent scans for trend awareness) ─────────────",
    ]

    if face_history:
        lines.append("  Face scans:")
        for doc in face_history:
            analysis = doc.get("analysis", {})
            created = doc.get("created_at")
            date_str = (
                created.strftime("%d %b %Y")
                if isinstance(created, datetime)
                else str(created)[:10] if created else "?"
            )
            score = analysis.get("overall_score", "?")
            hydration = analysis.get("hydration", "?")
            conds = [
                c.get("name", "?")
                for c in analysis.get("detected_condition", [])
                if isinstance(c, dict)
            ]
            cond_str = ", ".join(conds) if conds else "none detected"
            lines.append(
                f"    {date_str} — score: {score}/100, "
                f"hydration: {hydration}%, conditions: {cond_str}"
            )

    if scalp_history:
        lines.append("  Scalp/hair scans:")
        for doc in scalp_history:
            analysis = doc.get("analysis", {})
            created = doc.get("created_at")
            date_str = (
                created.strftime("%d %b %Y")
                if isinstance(created, datetime)
                else str(created)[:10] if created else "?"
            )
            score = analysis.get("overall_score", "?")
            conds = [
                c.get("name", "?")
                for c in analysis.get("detected_condition", [])
                if isinstance(c, dict)
            ]
            cond_str = ", ".join(conds) if conds else "none detected"
            lines.append(
                f"    {date_str} — score: {score}/100, conditions: {cond_str}"
            )

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_scalp_progress_context(scalp_progress: dict | None) -> str:
    """Format scalp scan progress comparison for motivational feedback."""
    if not scalp_progress:
        return ""

    delta = scalp_progress.get("score_delta", 0)
    direction = "improved" if delta > 0 else "declined" if delta < 0 else "unchanged"
    lines = [
        "── SCALP SCAN PROGRESS ──────────────────────────────────────────",
        f"  Score: {scalp_progress.get('previous_score', '?')} → "
        f"{scalp_progress.get('latest_score', '?')} ({direction} by {abs(delta)})",
    ]
    improved = scalp_progress.get("improved_conditions", [])
    worsened = scalp_progress.get("worsened_conditions", [])
    if improved:
        lines.append(f"  Improved: {', '.join(improved)}")
    if worsened:
        lines.append(f"  Needs attention: {', '.join(worsened)}")
    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_nutrition_context(
    nutritions: list[dict], foods: list[dict], recipes: list[dict],
) -> str:
    """
    Format nutrition / food / recipe recommendations based on detected conditions.
    Gives Gixy real dietary data to reference instead of generic advice.
    """
    if not nutritions and not foods and not recipes:
        return ""

    lines: list[str] = [
        "── PERSONALISED NUTRITION RECOMMENDATIONS (based on conditions) ─",
    ]

    if nutritions:
        lines.append("  Key Nutrients:")
        for n in nutritions:
            name = n.get("name", "?")
            benefits = n.get("benefits", "")
            lines.append(
                f"    • {name}: {benefits}" if benefits else f"    • {name}"
            )

    if foods:
        lines.append("  Recommended Foods:")
        for f_item in foods:
            name = f_item.get("name", "?")
            benefits = f_item.get("benefits", "")
            ingredients = f_item.get("ingredients", [])
            ing_str = (
                f" (contains: {', '.join(ingredients)})"
                if ingredients else ""
            )
            lines.append(
                f"    • {name}{ing_str}: {benefits}"
                if benefits else f"    • {name}{ing_str}"
            )

    if recipes:
        lines.append("  Recommended Recipes:")
        for r in recipes:
            name = r.get("name", "?")
            main_ing = r.get("main_ingredients", [])
            how = r.get("how_it_improves", "")
            ing_str = (
                f" (ingredients: {', '.join(main_ing)})"
                if main_ing else ""
            )
            lines.append(
                f"    • {name}{ing_str}: {how}"
                if how else f"    • {name}{ing_str}"
            )

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_routine_ingredients_context(
    routine: dict[str, list[dict]], product_scans: list[dict],
) -> str:
    """
    Cross-reference routine products with scanned product data so Gixy
    can answer ingredient-specific questions about the user's routine.
    """
    if not routine or not product_scans:
        return ""

    # Build lookup: lowercase product name → scan data
    scan_lookup: dict[str, dict] = {}
    for scan in product_scans:
        nested = scan.get("product") or {}
        name = scan.get("product_name") or nested.get("name", "")
        if name:
            scan_lookup[name.strip().lower()] = scan

    matches: list[dict] = []
    for slot in ("morning", "night", "weekly"):
        for step in routine.get(slot, []):
            pname = step.get("product_name", "").strip().lower()
            if pname in scan_lookup:
                scan = scan_lookup[pname]
                analysis = scan.get("analysis") or {}
                compat = (
                    scan.get("compatibility")
                    or analysis.get("compatibility", "")
                )
                score = (
                    scan.get("compatibility_score")
                    or analysis.get("compatibility_score")
                )
                good_ings = (
                    analysis.get("good_ingredients")
                    or scan.get("good_ingredients")
                    or []
                )
                bad_ings = (
                    analysis.get("bad_ingredients")
                    or scan.get("bad_ingredients")
                    or []
                )
                matches.append({
                    "name": step.get("product_name", pname),
                    "slot": slot,
                    "compatibility": compat,
                    "score": score,
                    "good_ingredients": good_ings[:5],
                    "bad_ingredients": bad_ings[:5],
                })

    if not matches:
        return ""

    lines: list[str] = [
        "── ROUTINE PRODUCT INGREDIENT ANALYSIS ──────────────────────────",
    ]
    for m in matches:
        score_str = f" ({m['score']}/100)" if m['score'] else ""
        lines.append(
            f"  • {m['name']} [{m['slot']}] — {m['compatibility']}{score_str}"
        )
        if m["good_ingredients"]:
            goods = ", ".join(
                str(g) if isinstance(g, str) else g.get("name", str(g))
                for g in m["good_ingredients"]
            )
            lines.append(f"      ✓ Good: {goods}")
        if m["bad_ingredients"]:
            bads = ", ".join(
                str(b) if isinstance(b, str) else b.get("name", str(b))
                for b in m["bad_ingredients"]
            )
            lines.append(f"      ✗ Avoid: {bads}")
    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


def _format_routine_check_context(check: dict | None) -> str:
    """
    Serialize the cached routine-check result into a compact prompt block so
    Gixy can discuss conflicts, allergy hits, pregnancy warnings, and budget.

    Expected structure (from GET /routine/check):
      {
        "conflicts":    [{"products": [...], "reason": str, "severity": str, "suggestion": str}],
        "allergy_hits": [{"product": str, "allergen": str, "severity": str, "suggestion": str}],
        "pregnancy_warnings": [{"product": str, "ingredient": str, "risk_level": str, "suggestion": str}],
        "budget_analysis": {"tier": str, "estimated_monthly_cost": num, "summary": str},
        "overall_safety_score": int,
        "summary": str,
      }
    """
    if not check:
        return ""

    # The cache document wraps the result inside a `result` key.
    data = check.get("result") or check

    conflicts          = data.get("conflicts")          or []
    allergy_hits       = data.get("allergy_hits")       or []
    pregnancy_warnings = data.get("pregnancy_warnings") or []
    budget             = data.get("budget_analysis")    or {}
    safety_score       = data.get("overall_safety_score")
    summary            = data.get("summary",            "")

    # Nothing meaningful to show
    if not (conflicts or allergy_hits or pregnancy_warnings or budget or summary):
        return ""

    lines: list[str] = [
        "── ROUTINE SAFETY CHECK (latest AI analysis) ────────────────────",
    ]

    if safety_score is not None:
        lines.append(f"  Overall safety score : {safety_score}/100")

    if summary:
        lines.append(f"  Summary : {summary}")

    if conflicts:
        lines.append("  Ingredient conflicts:")
        for c in conflicts:
            prods     = ", ".join(c.get("products") or [])
            reason    = c.get("reason",     "")
            severity  = c.get("severity",   "")
            suggest   = c.get("suggestion", "")
            lines.append(f"    ⚡ [{severity}] {prods}: {reason}")
            if suggest:
                lines.append(f"       → {suggest}")

    if allergy_hits:
        lines.append("  Allergy alerts:")
        for a in allergy_hits:
            product  = a.get("product",    "")
            allergen = a.get("allergen",   "")
            severity = a.get("severity",   "")
            suggest  = a.get("suggestion", "")
            lines.append(f"    🚨 [{severity}] {product} contains {allergen}")
            if suggest:
                lines.append(f"       → {suggest}")

    if pregnancy_warnings:
        lines.append("  Pregnancy warnings:")
        for p in pregnancy_warnings:
            product    = p.get("product",    "")
            ingredient = p.get("ingredient", "")
            risk       = p.get("risk_level", "")
            suggest    = p.get("suggestion", "")
            lines.append(f"    🤰 [{risk}] {product} — {ingredient}")
            if suggest:
                lines.append(f"       → {suggest}")

    if budget:
        tier     = budget.get("tier",                    "")
        cost     = budget.get("estimated_monthly_cost")
        b_summary = budget.get("summary",                "")
        cost_str = f"~${cost}/mo" if cost is not None else ""
        lines.append(f"  Budget analysis : {tier} {cost_str}")
        if b_summary:
            lines.append(f"    {b_summary}")

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_system_prompt(
    profile:             dict | None,
    memories:            list[str],
    face_scan:           dict | None,
    scalp_scan:          dict | None,
    routine:             dict[str, list[dict]],
    product_scans:       list[dict],
    profile_score:       dict | None,
    subscription:        str,
    scan_progress:       dict | None = None,
    scalp_progress:      dict | None = None,
    face_history:        list[dict] = None,
    scalp_history:       list[dict] = None,
    nutritions:          list[dict] = None,
    foods:               list[dict] = None,
    recipes:             list[dict] = None,
    routine_check:       dict | None = None,
) -> str:
    """
    Builds Gixy's system prompt across nine layers:

      1. Core role + personality
      2. Subscription status
      3. Skin/hair profile
      4. Profile score metrics
      5. Persistent memories
      6. Latest scan results (face + hair/scalp)
      6b. Scan history timeline (last 5 scans)
      7. Current routine
      7b. Personalised nutrition recommendations
      8. Recent product scans
      8b. Routine product ingredient analysis
      9. Scan progress (comparison)
      9b. Scalp scan progress (comparison)

    Every layer is optional — missing data produces an empty string and is
    silently omitted so the prompt stays clean.
    """

    # ── Layer 1 — Core role ───────────────────────────────────────────────────
    override_prompt = None
    try:
        from app.core.mongo_client import db
        config = db["ai_config"].find_one({"_id": "current"})
        if config and config.get("system_prompt_override"):
            override_prompt = config["system_prompt_override"]
    except Exception:
        pass

    if override_prompt:
        base = override_prompt
    else:
        base = """\
You are Gixy — a warm, motivational, and knowledgeable skincare coach \
embedded in the SkinSense app. You speak like a caring friend who genuinely \
wants the user to succeed. Keep responses concise (1-3 short paragraphs max).

Your personality:
  • Warm and supportive — celebrate progress, no matter how small
  • Educational — explain WHY things work, not just what to do
  • Data-driven — reference actual scan scores, conditions, and progress
  • Encouraging consistency — help users stick to their routine
  • Reassuring — never alarmist, always frame things positively

Your expertise covers:
  • Skincare routines and ingredient advice
  • Scalp and hair health
  • Product recommendations (ingredients to look for / avoid)
  • Explaining scan results in plain language
  • Nutrition and lifestyle tips for skin health
  • General dermatology education

Hard rules:
  • NEVER diagnose medical conditions — always say \
"this looks like it could be X, but please see a dermatologist to confirm."
  • NEVER recommend prescription medications.
  • Stay on topic — if the user asks about something unrelated to skin/hair/beauty \
wellness, politely redirect.
  • Be encouraging and positive, never alarmist.
  • Never pressure about subscriptions or purchases.
"""

    # ── Layer 2 — Subscription status ────────────────────────────────────────
    base += "\n" + _format_subscription_context(subscription) + "\n"
    base += (
        "If the user is on the Free plan and asks about a premium feature, "
        "you may mention it exists — but never be pushy or sales-like.\n"
    )

    # ── Layer 3 — Skin/hair profile ───────────────────────────────────────────
    if not profile:
        base += "\nNo skin profile available for this user — give general advice.\n"
    else:
        skin_type     = profile.get("skin_type")
        hair_type     = profile.get("hair_type")
        current_phase = profile.get("current_phase")
        skin_concerns = profile.get("skin_concerns") or []
        hair_concerns = profile.get("hair_concerns") or []
        allergies     = profile.get("allergies")     or []

        profile_lines: list[str] = []
        if skin_type:     profile_lines.append(f"  • Skin type:      {skin_type}")
        if hair_type:     profile_lines.append(f"  • Hair type:      {hair_type}")
        if current_phase: profile_lines.append(
            f"  • Current phase:  {current_phase.replace('_', ' ')}"
        )
        if skin_concerns: profile_lines.append(
            f"  • Skin concerns:  {', '.join(skin_concerns)}"
        )
        if hair_concerns: profile_lines.append(
            f"  • Hair concerns:  {', '.join(hair_concerns)}"
        )
        if allergies:     profile_lines.append(
            f"  • Allergies:      {', '.join(allergies)}"
        )

        if profile_lines:
            base += (
                "\n── USER SKIN PROFILE ────────────────────────────────────────────\n"
                + "\n".join(profile_lines)
                + "\n────────────────────────────────────────────────────────────────\n"
                "Always tailor your advice to this profile. Account for skin type, "
                "concerns, and allergies when recommending products or routines.\n"
            )

    # ── Layer 4 — Profile score metrics ──────────────────────────────────────
    score_ctx = _format_profile_score_context(profile_score)
    if score_ctx:
        base += (
            "\n" + score_ctx + "\n"
            "Reference these metrics to frame advice (e.g. if hydration is low, "
            "prioritise hydrating ingredients; if acne risk is High, steer away "
            "from comedogenic products).\n"
        )

    # ── Layer 5 — Persistent memories ────────────────────────────────────────
    if memories:
        bullet_block = "\n".join(f"  • {m}" for m in memories)
        base += (
            "\n── WHAT YOU REMEMBER ABOUT THIS USER (from past conversations) ───\n"
            + bullet_block
            + "\n────────────────────────────────────────────────────────────────\n"
            "Use these facts naturally. Do not announce that you 'remember' them — "
            "just weave them into your advice as if you've always known.\n"
        )

    # ── Layer 6 — Latest scan results ─────────────────────────────────────────
    scan_ctx = _format_scan_context(face_scan, scalp_scan)
    if scan_ctx:
        base += (
            "\n" + scan_ctx + "\n"
            "Reference these scan results when relevant. If the user asks about \n"
            "their skin or scalp health, connect your advice to these actual findings.\n"
        )

    # ── Layer 6b — Scan history ───────────────────────────────────────────────
    history_ctx = _format_scan_history_context(face_history or [], scalp_history or [])
    if history_ctx:
        base += (
            "\n" + history_ctx + "\n"
            "This shows the user's historical scores and conditions. Use it to "
            "recognise long-term trends and show the user you know their history.\n"
        )

    # ── Layer 7 — Current routine ─────────────────────────────────────────────
    routine_ctx = _format_routine_context(routine)
    if routine_ctx:
        base += (
            "\n" + routine_ctx + "\n"
            "The user's current routine is shown above. When recommending new products "
            "or steps, avoid duplicating what is already in their routine. "
            "If they ask about their routine, refer to these exact steps.\n"
        )
    else:
        base += (
            "\n── CURRENT ROUTINE ──────────────────────────────────────────────\n"
            "  (empty — this user has not added any routine steps yet)\n"
            "─────────────────────────────────────────────────────────────────\n"
            "Encourage them to build a routine after a scan, or suggest they "
            "use 'Generate Routine' after their next face or scalp scan.\n"
        )

    # ── Layer 7b — Personalised nutrition recommendations ────────────────────
    nutrition_ctx = _format_nutrition_context(nutritions or [], foods or [], recipes or [])
    if nutrition_ctx:
        base += (
            "\n" + nutrition_ctx + "\n"
            "Use these diet and nutrient suggestions to guide the user on "
            "internal wellness for their skin/hair conditions. Focus on "
            "whole foods and healthy recipes.\n"
        )

    # ── Layer 8 — Recent product scans ───────────────────────────────────────
    product_ctx = _format_product_scans_context(product_scans)
    if product_ctx:
        base += (
            "\n" + product_ctx + "\n"
            "If the user asks about a product they've scanned, refer to these "
            "results. Note the compatibility score and any flagged concerns.\n"
        )

    # ── Layer 8b — Routine product ingredient analysis ────────────────────────
    routine_ings_ctx = _format_routine_ingredients_context(routine, product_scans)
    if routine_ings_ctx:
        base += (
            "\n" + routine_ings_ctx + "\n"
            "Cross-reference this ingredient analysis when the user asks about "
            "ingredients in their routine or if certain products in their routine "
            "are compatible with their skin type.\n"
        )

    # ── Layer 9 — Scan progress ───────────────────────────────────────────────
    if scan_progress:
        delta = scan_progress.get("score_delta", 0)
        direction = "improved" if delta > 0 else "declined" if delta < 0 else "unchanged"
        progress_lines = [
            "── SCAN PROGRESS ────────────────────────────────────────────────",
            f"  Score: {scan_progress.get('previous_score', '?')} → {scan_progress.get('latest_score', '?')} ({direction} by {abs(delta)})",
            f"  Hydration: {scan_progress.get('previous_hydration', '?')}% → {scan_progress.get('latest_hydration', '?')}%",
        ]
        improved = scan_progress.get("improved_conditions", [])
        worsened = scan_progress.get("worsened_conditions", [])
        if improved:
            progress_lines.append(f"  Improved: {', '.join(improved)}")
        if worsened:
            progress_lines.append(f"  Needs attention: {', '.join(worsened)}")
        progress_lines.append("─────────────────────────────────────────────────────────────────")
        base += (
            "\n" + "\n".join(progress_lines) + "\n"
            "Reference this progress when motivating the user. Celebrate improvements "
            "and offer gentle, actionable advice for areas that need attention.\n"
        )

    # ── Layer 9b — Scalp scan progress ────────────────────────────────────────
    scalp_progress_ctx = _format_scalp_progress_context(scalp_progress)
    if scalp_progress_ctx:
        base += (
            "\n" + scalp_progress_ctx + "\n"
            "Reference this scalp scan progress to motivate the user on their hair "
            "and scalp health journey.\n"
        )

    # ── Layer 10 — Routine safety check ──────────────────────────────────────
    routine_check_ctx = _format_routine_check_context(routine_check)
    if routine_check_ctx:
        base += (
            "\n" + routine_check_ctx + "\n"
            "When the user asks about their routine safety, ingredient conflicts, "
            "allergy concerns, pregnancy-safe products, or budget, use the data "
            "above to give precise, personalised answers. Offer actionable "
            "suggestions from the 'suggestion' fields.\n"
        )

    return base


# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY SUMMARIZER (background task — unchanged from v2)
# ══════════════════════════════════════════════════════════════════════════════

_SUMMARIZER_SYSTEM = """\
You are a memory extractor for a skincare AI coach named Gixy.
You will be given the last 10 messages between a user and the AI.

Your job: extract 3 to 5 short, factual bullet points about the USER ONLY.
Focus on things that are useful for future skincare advice:
  - Products they mentioned using or wanting to try
  - Specific concerns they described (e.g. "breakouts on chin area")
  - Habits or lifestyle details they revealed (e.g. "washes face twice a day")
  - Preferences or dislikes they expressed (e.g. "hates heavy creams")
  - Any new symptoms or changes they described
  - Emotional state or motivation level

Rules:
  - Write each bullet as a plain fact starting with "User"
    e.g. "User is currently using a retinol serum every night."
  - Maximum 15 words per bullet.
  - Return ONLY a JSON array of strings. No markdown, no preamble.
    Example: ["User uses retinol nightly.", "User dislikes heavy moisturisers."]
  - If there are no useful facts to extract, return an empty array: []
""".strip()


async def _maybe_run_summarizer(user_id: str) -> None:
    """
    Fires after the user's message is saved.
    Triggers the background summariser every MEMORY_TRIGGER_EVERY user messages.
    Never blocks the streaming response.
    """
    try:
        total = await _count_user_messages(user_id)
        if total % MEMORY_TRIGGER_EVERY != 0:
            return
        logger.info(
            "Memory summariser triggered for user %s (total messages: %d)",
            user_id, total,
        )
        asyncio.create_task(_run_summarizer(user_id))
    except Exception as exc:
        logger.warning("Summariser trigger check failed for user %s: %s", user_id, exc)


async def _run_summarizer(user_id: str) -> None:
    """
    Reads the last MEMORY_TRIGGER_EVERY messages → extracts 3-5 bullets →
    appends them to users.user_memories, keeping newest MAX_MEMORY_BULLETS (200).
    Silently swallows all errors — this is non-critical background work.
    """
    try:
        db     = get_db()
        cursor = (
            db.chat_messages
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .limit(MEMORY_TRIGGER_EVERY)
        )
        recent_docs = await cursor.to_list(length=MEMORY_TRIGGER_EVERY)
        recent_docs.reverse()

        if not recent_docs:
            return

        conversation_text = "\n".join(
            f"{d['role'].upper()}: {d['content']}" for d in recent_docs
        )
        user_prompt = (
            f"Here are the last {len(recent_docs)} messages:\n\n"
            f"{conversation_text}\n\n"
            "Extract 3-5 memory bullets about the user. Return only the JSON array."
        )

        client = ClaudeClient()
        raw    = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = _SUMMARIZER_SYSTEM,
                user       = user_prompt,
                max_tokens = 256,
            ),
        )

        if not raw:
            return

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                l for l in cleaned.splitlines()
                if not l.strip().startswith("```")
            ).strip()

        bullets: list[str] = json.loads(cleaned)

        if not isinstance(bullets, list) or not bullets:
            return

        bullets = [
            str(b).strip()
            for b in bullets
            if isinstance(b, str) and b.strip()
        ][:5]

        if not bullets:
            return

        # Append and trim — $slice: -MAX_MEMORY_BULLETS keeps the NEWEST 200 bullets.
        try:
            await db.users.update_one(
                {"_id": ObjectId(user_id)},
                {
                    "$push": {
                        "user_memories": {
                            "$each":  bullets,
                            "$slice": -MAX_MEMORY_BULLETS,
                        }
                    }
                },
            )
        except Exception:
            await db.users.update_one(
                {"_id": user_id},
                {
                    "$push": {
                        "user_memories": {
                            "$each":  bullets,
                            "$slice": -MAX_MEMORY_BULLETS,
                        }
                    }
                },
            )

        logger.info(
            "Memory updated for user %s — added %d bullet(s): %s",
            user_id, len(bullets), bullets,
        )

    except Exception as exc:
        logger.warning("Memory summariser failed for user %s: %s", user_id, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  SSE STREAM GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

async def _stream_reply(
    user_id:  str,
    messages: list[dict],
    system:   str,
) -> AsyncGenerator[str, None]:
    """
    Streams the LLM reply token-by-token as Server-Sent Events.

    Each chunk:  data: {"chunk": "..."}\\n\\n
    End signal:  data: [DONE]\\n\\n
    Error event: data: {"error": "..."}\\n\\n
    """
    full_reply: list[str] = []

    try:
        async for text_chunk in async_stream_chat(system, messages, max_tokens=1024):
            full_reply.append(text_chunk)
            payload = json.dumps({"chunk": text_chunk}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    except Exception as exc:
        logger.error(
            "LLM stream error (%s): %s",
            "LM Studio" if USE_LOCAL_LLM else "Anthropic", exc,
        )

        if "connection" in str(exc).lower() or "refused" in str(exc).lower():
            msg = (
                "Cannot reach LM Studio. "
                "Make sure LM Studio is running and the Local Server is started."
                if USE_LOCAL_LLM
                else "Cannot reach the Anthropic API. Please retry."
            )
        elif "api key" in str(exc).lower() or "authentication" in str(exc).lower():
            msg = "Invalid API key." if not USE_LOCAL_LLM else "LM Studio auth error."
        elif "rate" in str(exc).lower():
            msg = "Rate limit hit. Please try again shortly."
        elif "timeout" in str(exc).lower():
            msg = "Request timed out. Please try again."
        else:
            msg = f"AI error: {exc}"

        yield f'data: {{"error": "{msg}"}}\n\n'
        return

    if full_reply:
        reply_text = "".join(full_reply)
        await _save_message(user_id, "assistant", reply_text)
        try:
            from app.services.usage_limiter import record_usage, estimate_tokens
            tokens = estimate_tokens(reply_text)
            await record_usage(user_id, "lia_chat", tokens_used=tokens)
        except Exception as e:
            logger.error(f"Failed to record usage for {user_id}: {e}")

    yield "data: [DONE]\n\n"


# ══════════════════════════════════════════════════════════════════════════════
#  SYNC REPLY COLLECTOR
# ══════════════════════════════════════════════════════════════════════════════

async def _collect_full_reply(
    user_id:  str,
    messages: list[dict],
    system:   str,
) -> str:
    """
    Drives the same async_stream_chat generator used by _stream_reply,
    but collects every chunk into a single string instead of yielding SSE events.

    Saves the completed assistant message to the DB exactly as _stream_reply does,
    so chat history is consistent regardless of which endpoint was used.

    Raises HTTPException 502 on any LLM error (mirrors _stream_reply error handling).
    """
    chunks: list[str] = []

    try:
        async for text_chunk in async_stream_chat(system, messages, max_tokens=1024):
            chunks.append(text_chunk)

    except Exception as exc:
        logger.error(
            "LLM sync error (%s): %s",
            "LM Studio" if USE_LOCAL_LLM else "Anthropic", exc,
        )
        exc_str = str(exc).lower()
        if "connection" in exc_str or "refused" in exc_str:
            msg = (
                "Cannot reach LM Studio. "
                "Make sure LM Studio is running and the Local Server is started."
                if USE_LOCAL_LLM
                else "Cannot reach the Anthropic API. Please retry."
            )
        elif "api key" in exc_str or "authentication" in exc_str:
            msg = "Invalid API key." if not USE_LOCAL_LLM else "LM Studio auth error."
        elif "rate" in exc_str:
            msg = "Rate limit hit. Please try again shortly."
        elif "timeout" in exc_str:
            msg = "Request timed out. Please try again."
        else:
            msg = f"AI error: {exc}"
        raise HTTPException(status_code=502, detail=msg)

    full_text = "".join(chunks)

    if full_text:
        await _save_message(user_id, "assistant", full_text)
        try:
            from app.services.usage_limiter import record_usage, estimate_tokens
            tokens = estimate_tokens(full_text)
            await record_usage(user_id, "lia_chat", tokens_used=tokens)
        except Exception as e:
            logger.error(f"Failed to record usage for {user_id}: {e}")

    return full_text


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/message",
    summary     = "Send a chat message to Lia (SSE streaming)",
    description = (
        "Send a message to Lia, your AI skincare coach. "
        "The reply streams back as Server-Sent Events.\n\n"
        "**SSE format:**\n"
        "```\n"
        'data: {"chunk": "Hello"}\n'
        'data: {"chunk": " there"}\n'
        "data: [DONE]\n"
        "```\n\n"
        "**Lia's context includes:**\n"
        "- Subscription status\n"
        "- Skin & hair profile\n"
        "- Profile score metrics\n"
        "- Persistent memory (up to 200 bullets)\n"
        "- Latest scan results + progress comparison\n"
        "- Current routine steps\n"
        "- Recent product scan history\n\n"
        f"**Active LLM backend:** {'🏠 LM Studio (local)' if USE_LOCAL_LLM else '☁️ Anthropic Claude'}"
    ),
)
async def send_message(
    payload: ChatMessageRequest,
    user_id: str = Depends(_get_current_user_id),
):
    user_text = payload.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    # ── Usage Limit Check ──────────────────────────────────────────────────────
    from app.services.usage_limiter import check_usage_limit, record_usage
    subscription_status = await _get_subscription_status(user_id)
    user_plan = "premium" if subscription_status in ("premium", "trialing") else "free"
    await check_usage_limit(user_id, "lia_chat", user_plan)

    # ── 1. Save user message ──────────────────────────────────────────────────
    await _save_message(user_id, "user", user_text)

    # ── 2. Trigger memory summariser (fire-and-forget) ────────────────────────
    await _maybe_run_summarizer(user_id)

    # ── 3. Fetch ALL context in parallel ──────────────────────────────────────
    (
        profile,
        memories,
        (face_scan, scalp_scan),
        history,
        routine,
        product_scans,
        subscription,
        scan_progress,
        scalp_progress,
        (face_history, scalp_history),
        routine_check,
    ) = await asyncio.gather(
        _get_user_profile(user_id),
        _get_user_memories(user_id),
        _get_latest_scans(user_id),
        _load_history(user_id, limit=20),
        _get_routine_steps(user_id),
        _get_product_scans(user_id, limit=20),
        _get_subscription_status(user_id),
        _get_scan_progress(user_id),
        _get_scalp_progress(user_id),
        _get_scan_history(user_id, face_limit=5, scalp_limit=5),
        _get_latest_cached_routine_check(user_id),
    )

    profile_score = _compute_profile_score(profile)

    # Fetch nutrition recommendations based on detected conditions
    detected_conditions = []
    for scan in (face_scan, scalp_scan):
        if scan:
            analysis = scan.get("analysis", {})
            for cond in analysis.get("detected_condition", []):
                if isinstance(cond, dict) and cond.get("name"):
                    detected_conditions.append(cond["name"])

    # Unique conditions, keeping order
    seen = set()
    unique_conditions = []
    for c in detected_conditions:
        if c not in seen:
            seen.add(c)
            unique_conditions.append(c)

    nutritions, foods, recipes = await _get_nutrition_recommendations(unique_conditions)

    # ── 4. Build enriched system prompt ──────────────────────────────────────
    system = _build_system_prompt(
        profile        = profile,
        memories       = memories,
        face_scan      = face_scan,
        scalp_scan     = scalp_scan,
        routine        = routine,
        product_scans  = product_scans,
        profile_score  = profile_score,
        subscription   = subscription,
        scan_progress  = scan_progress,
        scalp_progress = scalp_progress,
        face_history   = face_history,
        scalp_history  = scalp_history,
        nutritions     = nutritions,
        foods          = foods,
        recipes        = recipes,
        routine_check  = routine_check,
    )

    # ── 5. Stream the reply ───────────────────────────────────────────────────
    return StreamingResponse(
        _stream_reply(user_id, history, system),
        media_type = "text/event-stream",
        headers    = {
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )



@router.post(
    "/message/sync",
    response_model = ChatSyncResponse,
    summary        = "Send a chat message to Lia (full JSON reply)",
    description    = (
        "Send a message to Lia. Waits for the complete response and returns it "
        "as a single JSON object — no streaming, no SSE.\n\n"
        "**Response shape:**\n"
        "```json\n"
        '{"reply": "Full Lia response text goes here"}\n'
        "```\n\n"
        "Identical to `POST /chat/message` in every other way:\n"
        "- Same 9-layer system prompt (profile, scans, routine, product scans, "
        "memories, subscription status, profile score, scan progress)\n"
        "- Same chat history context (last 20 messages)\n"
        "- Same memory summariser (fires every 10 user messages)\n"
        "- Same DB save — the reply is stored in chat_messages so history is consistent\n\n"
        "Use this endpoint when your client cannot consume SSE streams "
        "(e.g. REST clients, background jobs, testing).\n\n"
        f"**Active LLM backend:** {'🏠 LM Studio (local)' if USE_LOCAL_LLM else '☁️ Anthropic Claude'}"
    ),
)
async def send_message_sync(
    payload: ChatMessageRequest,
    user_id: str = Depends(_get_current_user_id),
) -> ChatSyncResponse:
    user_text = payload.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    # ── Usage Limit Check ──────────────────────────────────────────────────────
    from app.services.usage_limiter import check_usage_limit, record_usage
    subscription_status = await _get_subscription_status(user_id)
    user_plan = "premium" if subscription_status in ("premium", "trialing") else "free"
    await check_usage_limit(user_id, "lia_chat", user_plan)

    # ── 1. Save user message ──────────────────────────────────────────────────
    await _save_message(user_id, "user", user_text)

    # ── 2. Trigger memory summariser (fire-and-forget) ────────────────────────
    await _maybe_run_summarizer(user_id)

    # ── 3. Fetch ALL context in parallel ──────────────────────────────────────
    (
        profile,
        memories,
        (face_scan, scalp_scan),
        history,
        routine,
        product_scans,
        subscription,
        scan_progress,
        scalp_progress,
        (face_history, scalp_history),
        routine_check,
    ) = await asyncio.gather(
        _get_user_profile(user_id),
        _get_user_memories(user_id),
        _get_latest_scans(user_id),
        _load_history(user_id, limit=20),
        _get_routine_steps(user_id),
        _get_product_scans(user_id, limit=20),
        _get_subscription_status(user_id),
        _get_scan_progress(user_id),
        _get_scalp_progress(user_id),
        _get_scan_history(user_id, face_limit=5, scalp_limit=5),
        _get_latest_cached_routine_check(user_id),
    )

    profile_score = _compute_profile_score(profile)

    # Fetch nutrition recommendations based on detected conditions
    detected_conditions = []
    for scan in (face_scan, scalp_scan):
        if scan:
            analysis = scan.get("analysis", {})
            for cond in analysis.get("detected_condition", []):
                if isinstance(cond, dict) and cond.get("name"):
                    detected_conditions.append(cond["name"])

    # Unique conditions, keeping order
    seen = set()
    unique_conditions = []
    for c in detected_conditions:
        if c not in seen:
            seen.add(c)
            unique_conditions.append(c)

    nutritions, foods, recipes = await _get_nutrition_recommendations(unique_conditions)

    # ── 4. Build enriched system prompt ──────────────────────────────────────
    system = _build_system_prompt(
        profile        = profile,
        memories       = memories,
        face_scan      = face_scan,
        scalp_scan     = scalp_scan,
        routine        = routine,
        product_scans  = product_scans,
        profile_score  = profile_score,
        subscription   = subscription,
        scan_progress  = scan_progress,
        scalp_progress = scalp_progress,
        face_history   = face_history,
        scalp_history  = scalp_history,
        nutritions     = nutritions,
        foods          = foods,
        recipes        = recipes,
        routine_check  = routine_check,
    )

    # ── 5. Collect the full reply and return as JSON ──────────────────────────
    reply = await _collect_full_reply(user_id, history, system)
    return ChatSyncResponse(reply=reply)


@router.get(
    "/history",
    response_model = ChatHistoryResponse,
    summary        = "Get chat history",
)
async def get_history(
    limit:   int = Query(default=50, ge=1, le=200),
    offset:  int = Query(default=0, ge=0),
    user_id: str = Depends(_get_current_user_id),
):
    db = get_db()
    total = await db.chat_messages.count_documents({"user_id": user_id})
    cursor = (
        db.chat_messages
        .find({"user_id": user_id})
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    docs.reverse()

    messages = [
        ChatMessage(
            role       = d["role"],
            content    = d["content"],
            created_at = d["created_at"],
        )
        for d in docs
    ]
    return ChatHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        messages=messages
    )


@router.delete("/history", summary="Clear chat history")
async def clear_history(user_id: str = Depends(_get_current_user_id)):
    db     = get_db()
    result = await db.chat_messages.delete_many({"user_id": user_id})
    return {"success": True, "deleted": result.deleted_count}
