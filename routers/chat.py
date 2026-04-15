# routers/chat.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — AI Chatbot  (v2 — Persistent Memory)        ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /chat/message   → Send a message (SSE streaming reply)   ║
║   GET  /chat/history   → Get last N messages                    ║
║   DELETE /chat/history → Clear all chat history                 ║
╚══════════════════════════════════════════════════════════════════╝

What's new in v2
────────────────
1. SCAN HISTORY INJECTION
   The most recent face scan + most recent hair/scalp scan are fetched
   from `scan_results` and injected into the system prompt on every
   message. The AI knows the user's latest scores, advice, and triggers
   without the user having to repeat themselves.

2. SUMMARIZATION MEMORY
   Every time the user's total message count crosses a multiple of 10,
   a background task fires. It reads the last 10 chat messages and asks
   the AI to extract 3-5 concrete facts about the user (products they
   mentioned, concerns they shared, preferences they revealed, etc.).
   Those facts are appended to a `user_memories` list stored directly
   in the users MongoDB document.

3. MEMORY INJECTION
   All accumulated `user_memories` bullets are prepended to the system
   prompt so the AI always remembers facts from past conversations.

MongoDB changes (no migration needed — new fields start empty):
  users.user_memories   → list[str]  (bullet-point facts, max 60 total)

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

# ── How often to run the memory summarizer (every N user messages) ─────────
MEMORY_TRIGGER_EVERY = 10
# Hard cap on accumulated memory bullets to prevent unbounded growth
MAX_MEMORY_BULLETS   = 60


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
    Fetch the single most recent face scan and the single most recent
    hair/scalp scan for this user from `scan_results`.

    Returns (face_scan_doc, hair_scalp_scan_doc).
    Either can be None if the user has no scan of that type yet.

    NOTE: scan_results stores user_id as a plain string (not ObjectId),
          so we query with str(user_id) directly.
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
    Returns [] if the field doesn't exist yet (new users).
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
    """Count total messages the user (role='user') has sent across all time."""
    db = get_db()
    return await db.chat_messages.count_documents(
        {"user_id": user_id, "role": "user"}
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN CONTEXT FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def _format_scan_context(face: dict | None, scalp: dict | None) -> str:
    """
    Convert raw scan documents into a compact, readable text block for
    injection into the system prompt.

    Only includes fields the AI actually needs: score, advice, and a
    summary of detected triggers with their severity levels.
    Returns an empty string if both scans are None.
    """
    if not face and not scalp:
        return ""

    lines: list[str] = [
        "── LATEST SCAN RESULTS (inject as factual context) ──────────────",
    ]

    def _fmt_scan(doc: dict, label: str) -> list[str]:
        out: list[str] = []
        scanned_at = doc.get("scanned_at")
        date_str   = (
            scanned_at.strftime("%d %b %Y")
            if isinstance(scanned_at, datetime)
            else str(scanned_at)[:10]
        )
        out.append(f"  {label} scan  (date: {date_str}, score: {doc.get('score', '?')}/100)")
        out.append(f"  Advice given: {doc.get('advice', 'N/A')}")

        triggers = doc.get("detected_triggers", [])
        if triggers:
            out.append("  Detected triggers:")
            for t in triggers:
                name  = t.get("trigger_name", "Unknown")
                level = t.get("trigger_level", "low")
                cure  = t.get("cure_advice", "")
                out.append(f"    • {name} [{level}] — {cure}")
        return out

    if face:
        lines.extend(_fmt_scan(face, "Face"))
    if scalp:
        if face:
            lines.append("")   # blank line between the two blocks
        lines.extend(_fmt_scan(scalp, "Hair/Scalp"))

    lines.append("─────────────────────────────────────────────────────────────────")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_system_prompt(
    profile:   dict | None,
    memories:  list[str],
    face_scan: dict | None,
    scalp_scan: dict | None,
) -> str:
    """
    Builds the full system prompt with four layers:
      1. Core role definition + hard rules
      2. User skin/hair profile
      3. Persistent memories (facts extracted from past conversations)
      4. Latest scan results (face + hair/scalp)
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

    # ── Layer 2 — Skin/hair profile ───────────────────────────────────────────
    if not profile:
        base += "\nNo skin profile available for this user — give general advice.\n"
    else:
        skin_type     = profile.get("skin_type")
        hair_type     = profile.get("hair_type")
        current_phase = profile.get("current_phase")
        skin_concerns = profile.get("skin_concerns") or []
        hair_concerns = profile.get("hair_concerns") or []
        allergies     = profile.get("allergies") or []

        profile_lines: list[str] = []
        if skin_type:      profile_lines.append(f"  • Skin type:      {skin_type}")
        if hair_type:      profile_lines.append(f"  • Hair type:      {hair_type}")
        if current_phase:  profile_lines.append(f"  • Current phase:  {current_phase.replace('_', ' ')}")
        if skin_concerns:  profile_lines.append(f"  • Skin concerns:  {', '.join(skin_concerns)}")
        if hair_concerns:  profile_lines.append(f"  • Hair concerns:  {', '.join(hair_concerns)}")
        if allergies:      profile_lines.append(f"  • Allergies:      {', '.join(allergies)}")

        if profile_lines:
            base += (
                "\n── USER SKIN PROFILE ────────────────────────────────────────────\n"
                + "\n".join(profile_lines)
                + "\n────────────────────────────────────────────────────────────────\n"
                "Always tailor your advice to this profile. Account for skin type, "
                "concerns, and allergies when recommending products or routines.\n"
            )

    # ── Layer 3 — Persistent memories ────────────────────────────────────────
    if memories:
        bullet_block = "\n".join(f"  • {m}" for m in memories)
        base += (
            "\n── WHAT YOU REMEMBER ABOUT THIS USER (from past conversations) ───\n"
            + bullet_block
            + "\n────────────────────────────────────────────────────────────────\n"
            "Use these facts naturally. Do not announce that you 'remember' them — "
            "just weave them into your advice as if you've always known.\n"
        )

    # ── Layer 4 — Latest scan results ─────────────────────────────────────────
    scan_context = _format_scan_context(face_scan, scalp_scan)
    if scan_context:
        base += (
            "\n" + scan_context + "\n"
            "Reference these scan results when relevant. If the user asks about \n"
            "their skin or scalp health, connect your advice to these actual findings.\n"
        )

    return base


# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY SUMMARIZER (background task)
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
    Checks if the user's total message count is a multiple of MEMORY_TRIGGER_EVERY.
    If so, runs the summarizer in the background (fire-and-forget).

    This is called AFTER the user's message is saved, so the count already
    includes the message that just triggered the check.
    """
    try:
        total = await _count_user_messages(user_id)
        if total % MEMORY_TRIGGER_EVERY != 0:
            return

        logger.info(
            "Memory summarizer triggered for user %s (total messages: %d)",
            user_id, total,
        )
        asyncio.create_task(_run_summarizer(user_id))

    except Exception as exc:
        # Never let a summarizer error affect the main chat flow
        logger.warning("Summarizer trigger check failed for user %s: %s", user_id, exc)


async def _run_summarizer(user_id: str) -> None:
    """
    Reads the last MEMORY_TRIGGER_EVERY messages, asks the AI to extract
    memory bullets, then appends them to users.user_memories.

    Silently swallows all errors — this is non-critical background work.
    """
    try:
        # ── 1. Load the last 10 messages ──────────────────────────────────────
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

        # ── 2. Format messages for the summarizer ─────────────────────────────
        conversation_text = "\n".join(
            f"{d['role'].upper()}: {d['content']}" for d in recent_docs
        )
        user_prompt = (
            f"Here are the last {len(recent_docs)} messages:\n\n"
            f"{conversation_text}\n\n"
            "Extract 3-5 memory bullets about the user. Return only the JSON array."
        )

        # ── 3. Call the AI (sync ClaudeClient in a thread) ────────────────────
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

        # ── 4. Parse the JSON array ───────────────────────────────────────────
        # Strip accidental markdown fences
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                l for l in cleaned.splitlines()
                if not l.strip().startswith("```")
            ).strip()

        bullets: list[str] = json.loads(cleaned)

        if not isinstance(bullets, list) or not bullets:
            return

        # Keep only non-empty strings, max 15 words each
        bullets = [
            str(b).strip()
            for b in bullets
            if isinstance(b, str) and b.strip()
        ][:5]

        if not bullets:
            return

        # ── 5. Append to users.user_memories (capped at MAX_MEMORY_BULLETS) ──
        # We use $push + $slice to atomically append and trim in one operation.
        # $slice: -MAX_MEMORY_BULLETS keeps the LAST N elements (newest memories).
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
        logger.warning("Memory summarizer failed for user %s: %s", user_id, exc)


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

    Each chunk:  data: {"chunk": "..."}\n\n
    End signal:  data: [DONE]\n\n
    Error event: data: {"error": "..."}\n\n
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

    # Save assistant reply after stream completes
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
        "The system prompt automatically includes:\n"
        "- User's skin/hair profile\n"
        "- Facts remembered from past conversations (persistent memory)\n"
        "- Latest face scan result\n"
        "- Latest hair/scalp scan result\n\n"
        "Chat history (last 20 messages) is included for continuity. "
        "A background memory summarizer runs every 10 user messages.\n\n"
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

    # ── 2. Trigger memory summarizer if needed (fire-and-forget) ─────────────
    #    We run this AFTER saving the message so the count includes this message.
    #    _maybe_run_summarizer internally calls asyncio.create_task so it never
    #    blocks the streaming response.
    await _maybe_run_summarizer(user_id)

    # ── 3. Load all context in parallel ──────────────────────────────────────
    profile_task   = asyncio.create_task(_get_user_profile(user_id))
    memories_task  = asyncio.create_task(_get_user_memories(user_id))
    scans_task     = asyncio.create_task(_get_latest_scans(user_id))
    history_task   = asyncio.create_task(_load_history(user_id, limit=20))

    profile, memories, (face_scan, scalp_scan), history = await asyncio.gather(
        profile_task,
        memories_task,
        scans_task,
        history_task,
    )

    # ── 4. Build enriched system prompt ──────────────────────────────────────
    system = _build_system_prompt(
        profile    = profile,
        memories   = memories,
        face_scan  = face_scan,
        scalp_scan = scalp_scan,
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