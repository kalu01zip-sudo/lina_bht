# routers/chat.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — AI Chatbot  (v3 — Full User Context)        ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /chat/message   → Send a message (SSE streaming reply)   ║
║   GET  /chat/history   → Get last N messages                    ║
║   DELETE /chat/history → Clear all chat history                 ║
╚══════════════════════════════════════════════════════════════════╝

What's new in v3
────────────────
1. FULL USER CONTEXT
   In addition to the existing profile + scan + memory layers, GIXY
   now receives on every message:
     • Current routine steps (morning / night / weekly)
     • Recent product scan history (last 3)
     • Profile score metrics (care score, hydration %, acne risk, sensitivity)
     • Subscription status (free / trialing / premium)

   All six data sources are fetched in a single parallel asyncio.gather,
   so latency impact is minimal.

2. MEMORY CAP RAISED  60 → 200
   The rolling window keeps the 200 most recent memory bullets.
   The summariser still fires every 10 user messages and appends
   3-5 bullets per run.

3. STRICT USER ISOLATION
   Every DB query is scoped to the authenticated user's _id.
   Kalu's data is never visible in Lalu's context, and vice versa.

SSE streaming format (unchanged):
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
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from database import get_db
from claude_client import async_stream_chat, ClaudeClient, USE_LOCAL_LLM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Chatbot"])

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
    messages: list[ChatMessage]


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
    scan_results stores user_id as a plain string — query with str(user_id).
    """
    db      = get_db()
    uid_str = str(user_id)

    face_doc, scalp_doc = await asyncio.gather(
        db.scan_results.find_one(
            {"user_id": uid_str, "scan_type": "face"},
            sort=[("scanned_at", -1)],
        ),
        db.scan_results.find_one(
            {"user_id": uid_str, "scan_type": "hair_scalp"},
            sort=[("scanned_at", -1)],
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

    Returns a dict with keys "morning", "night", "weekly".
    Each value is a list of lightweight step dicts:
      { product_name, title (optional), bio (optional) }

    Steps are returned in their saved order (order field ascending).
    """
    db   = get_db()
    docs = await (
        db.routine_steps
        .find({"user_id": user_id})
        .sort("order", 1)
        .to_list(200)
    )
    grouped: dict[str, list[dict]] = {"morning": [], "night": [], "weekly": []}
    for doc in docs:
        slot = doc.get("time_slot", "morning")
        if slot not in grouped:
            continue
        grouped[slot].append({
            "product_name": doc.get("product_name", ""),
            "title":        doc.get("title")       or None,
            "bio":          doc.get("bio") or doc.get("instructions") or None,
        })
    return grouped


async def _get_product_scans(user_id: str, limit: int = 3) -> list[dict]:
    """
    Fetch the most recent product scans for this user (newest first).
    Returns an empty list if the user has never scanned a product.
    """
    db      = get_db()
    uid_str = str(user_id)
    docs    = await (
        db.scan_results
        .find({"user_id": uid_str, "scan_type": "product"})
        .sort("scanned_at", -1)
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
        from profile_scorer import ProfileScorer, UserProfile  # local import — avoids circular deps
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
    Only includes fields the AI needs: score, advice, triggers and their severity.
    """
    if not face and not scalp:
        return ""

    lines: list[str] = [
        "── LATEST SCAN RESULTS ──────────────────────────────────────────",
    ]

    def _fmt_scan(doc: dict, label: str) -> list[str]:
        out: list[str] = []
        scanned_at = doc.get("scanned_at")
        date_str   = (
            scanned_at.strftime("%d %b %Y")
            if isinstance(scanned_at, datetime)
            else str(scanned_at)[:10]
        )
        out.append(
            f"  {label} scan  (date: {date_str}, score: {doc.get('score', '?')}/100)"
        )
        out.append(f"  Advice given: {doc.get('advice', 'N/A')}")

        triggers = doc.get("detected_triggers", [])
        if triggers:
            out.append("  Detected triggers:")
            for t in triggers:
                name  = t.get("trigger_name",  "Unknown")
                level = t.get("trigger_level", "low")
                cure  = t.get("cure_advice",   "")
                out.append(f"    • {name} [{level}] — {cure}")
        return out

    if face:
        lines.extend(_fmt_scan(face,  "Face"))
    if scalp:
        if face:
            lines.append("")
        lines.extend(_fmt_scan(scalp, "Hair/Scalp"))

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
        scanned_at = scan.get("scanned_at", "")
        date_str   = (
            scanned_at.strftime("%d %b %Y")
            if isinstance(scanned_at, datetime)
            else str(scanned_at)[:10]
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
    GIXY uses this to know which premium features to mention.
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


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_system_prompt(
    profile:       dict | None,
    memories:      list[str],
    face_scan:     dict | None,
    scalp_scan:    dict | None,
    routine:       dict[str, list[dict]],
    product_scans: list[dict],
    profile_score: dict | None,
    subscription:  str,
) -> str:
    """
    Builds the full GIXY system prompt across eight layers:

      1. Core role + hard rules
      2. Subscription status          ← NEW in v3
      3. Skin/hair profile
      4. Profile score metrics        ← NEW in v3
      5. Persistent memories
      6. Latest scan results (face + hair/scalp)
      7. Current routine              ← NEW in v3
      8. Recent product scans         ← NEW in v3

    Every layer is optional — missing data produces an empty string and is
    silently omitted so the prompt stays clean.
    """

    # ── Layer 1 — Core role ───────────────────────────────────────────────────
    base = """\
You are GIXY — a friendly, professional skincare and haircare assistant \
embedded in the SkinSense app. You speak like a knowledgeable friend, not a \
clinical robot. Keep responses concise (1-3 short paragraphs max).

Your expertise covers:
  • Skincare routines and ingredient advice
  • Scalp and hair health
  • Product recommendations (ingredients to look for / avoid)
  • Explaining scan results in plain language
  • General dermatology education

Hard rules:
  • NEVER diagnose medical conditions — always say \
"this looks like it could be X, but please see a dermatologist to confirm."
  • NEVER recommend prescription medications.
  • Stay on topic — if the user asks about something unrelated to skin/hair/beauty \
wellness, politely redirect.
  • Be encouraging and positive, never alarmist.
"""

    # ── Layer 2 — Subscription status ────────────────────────────────────────
    base += "\n" + _format_subscription_context(subscription) + "\n"
    base += (
        "If the user is on the Free plan and asks about a premium feature, "
        "gently mention they can upgrade to unlock it — but never be pushy.\n"
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

    # ── Layer 8 — Recent product scans ───────────────────────────────────────
    product_ctx = _format_product_scans_context(product_scans)
    if product_ctx:
        base += (
            "\n" + product_ctx + "\n"
            "If the user asks about a product they've scanned, refer to these "
            "results. Note the compatibility score and any flagged concerns.\n"
        )

    return base


# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY SUMMARIZER (background task — unchanged from v2)
# ══════════════════════════════════════════════════════════════════════════════

_SUMMARIZER_SYSTEM = """\
You are a memory extractor for a skincare AI assistant named GIXY.
You will be given the last 10 messages between a user and the AI.

Your job: extract 3 to 5 short, factual bullet points about the USER ONLY.
Focus on things that are useful for future skincare advice:
  - Products they mentioned using or wanting to try
  - Specific concerns they described (e.g. "breakouts on chin area")
  - Habits or lifestyle details they revealed (e.g. "washes face twice a day")
  - Preferences or dislikes they expressed (e.g. "hates heavy creams")
  - Any new symptoms or changes they described

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
        await _save_message(user_id, "assistant", "".join(full_reply))

    yield "data: [DONE]\n\n"


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/message",
    summary     = "Send a chat message (SSE streaming)",
    description = (
        "Send a message to GIXY. The reply streams back as Server-Sent Events.\n\n"
        "**SSE format:**\n"
        "```\n"
        'data: {"chunk": "Hello"}\n'
        'data: {"chunk": " there"}\n'
        "data: [DONE]\n"
        "```\n\n"
        "**GIXY's system prompt is rebuilt on every message and includes:**\n"
        "- Subscription status (free / trial / premium)\n"
        "- Skin & hair profile\n"
        "- Profile score metrics (care score, hydration %, acne risk, sensitivity)\n"
        "- Facts remembered from past conversations (persistent memory — up to 200 bullets)\n"
        "- Latest face scan result\n"
        "- Latest hair/scalp scan result\n"
        "- Current routine steps (morning / night / weekly)\n"
        "- Recent product scan history (last 3)\n\n"
        "All six data sources are fetched in parallel on every request, so GIXY "
        "always reflects the latest state of the user's data without any manual refresh.\n\n"
        "Chat history (last 20 messages) is included for conversational continuity. "
        "A background memory summariser runs every 10 user messages.\n\n"
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

    # ── 1. Save user message ──────────────────────────────────────────────────
    await _save_message(user_id, "user", user_text)

    # ── 2. Trigger memory summariser (fire-and-forget) ────────────────────────
    await _maybe_run_summarizer(user_id)

    # ── 3. Fetch ALL context in parallel ──────────────────────────────────────
    #
    #  Six independent DB operations run concurrently via asyncio.gather.
    #  Total latency ≈ max(individual latency) rather than sum — typically
    #  well under 100 ms on a local MongoDB instance.
    #
    #  Strict user isolation: every loader filters by user_id, so Kalu's
    #  gather can never return Lalu's data.
    #
    (
        profile,
        memories,
        (face_scan, scalp_scan),
        history,
        routine,
        product_scans,
        subscription,
    ) = await asyncio.gather(
        _get_user_profile(user_id),
        _get_user_memories(user_id),
        _get_latest_scans(user_id),
        _load_history(user_id, limit=20),
        _get_routine_steps(user_id),
        _get_product_scans(user_id, limit=3),
        _get_subscription_status(user_id),
    )

    # Profile score is computed locally (zero DB / API calls) from the profile
    # we already fetched above — no extra await needed.
    profile_score = _compute_profile_score(profile)

    # ── 4. Build enriched system prompt ──────────────────────────────────────
    system = _build_system_prompt(
        profile       = profile,
        memories      = memories,
        face_scan     = face_scan,
        scalp_scan    = scalp_scan,
        routine       = routine,
        product_scans = product_scans,
        profile_score = profile_score,
        subscription  = subscription,
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


@router.get(
    "/history",
    response_model = ChatHistoryResponse,
    summary        = "Get chat history",
)
async def get_history(
    limit:   int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(_get_current_user_id),
):
    db = get_db()
    cursor = (
        db.chat_messages
        .find({"user_id": user_id})
        .sort("created_at", -1)
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
    return ChatHistoryResponse(messages=messages)


@router.delete("/history", summary="Clear chat history")
async def clear_history(user_id: str = Depends(_get_current_user_id)):
    db     = get_db()
    result = await db.chat_messages.delete_many({"user_id": user_id})
    return {"success": True, "deleted": result.deleted_count}