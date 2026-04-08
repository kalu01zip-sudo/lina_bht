# routers/chat.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — AI Chatbot                                  ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /chat/message   → Send a message (SSE streaming reply)   ║
║   GET  /chat/history   → Get last N messages                    ║
║   DELETE /chat/history → Clear all chat history                 ║
╚══════════════════════════════════════════════════════════════════╝

SSE streaming: each chunk arrives as:
  data: {"chunk": "Hello"}
  data: {"chunk": " there"}
  data: [DONE]

The full assistant reply is also saved to MongoDB after streaming ends.

Auth: Bearer JWT — same token your other routes use.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import anthropic
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Chatbot"])

# ── Auth (re-uses the same JWT pattern as your other routes) ─────────────────
# If your existing auth router already exposes a `get_current_user` dependency,
# replace the import below with:
#   from routers.auth import get_current_user
# and remove the duplicate logic here.

import jwt as pyjwt  # pip install PyJWT

_bearer = HTTPBearer()

def _get_current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Validates the Bearer JWT and returns the user_id (MongoDB _id as string).
    Raises 401 on any failure.
    """
    secret = os.getenv("SECRET_KEY", "")
    token  = creds.credentials
    try:
        payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        uid = payload.get("sub") or payload.get("user_id") or payload.get("id")
        if not uid:
            raise ValueError("No user identifier in token")
        return str(uid)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ── Request / Response schemas ────────────────────────────────────────────────

class ChatMessageRequest(BaseModel):
    message: str

    model_config = {
        "json_schema_extra": {
            "example": {"message": "What ingredients should I avoid for oily skin?"}
        }
    }


class ChatMessage(BaseModel):
    role:       str       # "user" | "assistant"
    content:    str
    created_at: datetime


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


# ── System prompt builder ─────────────────────────────────────────────────────

def _build_system_prompt(profile: dict | None) -> str:
    """
    Builds a personalised system prompt by injecting the user's skin profile.
    If no profile exists yet, the bot still works but gives generic advice.
    """
    base = """\
You are SkinSense AI — a friendly, professional skincare and haircare assistant \
embedded in the SkinSense app. You speak like a knowledgeable friend, not a \
clinical robot. Keep responses concise (2-4 short paragraphs max).

Your expertise covers:
  • Skincare routines and ingredient advice
  • Scalp and hair health
  • Product recommendations (ingredients to look for / avoid)
  • Explaining scan results in plain language
  • General dermatology education

Hard rules:
  • NEVER diagnose medical conditions (eczema, psoriasis, etc.) — always say \
"this looks like it could be X, but please see a dermatologist to confirm."
  • NEVER recommend prescription medications.
  • Stay on topic — if the user asks about something unrelated to skin/hair/beauty \
wellness, politely redirect.
  • Be encouraging and positive, never alarmist.
"""

    if not profile:
        return base + "\nNo skin profile is available yet for this user — give general advice."

    # Inject whatever profile fields exist
    skin_type     = profile.get("skin_type")
    hair_type     = profile.get("hair_type")
    current_phase = profile.get("current_phase")
    skin_concerns = profile.get("skin_concerns") or []
    hair_concerns = profile.get("hair_concerns") or []
    allergies     = profile.get("allergies") or []

    profile_lines = []
    if skin_type:      profile_lines.append(f"  • Skin type:      {skin_type}")
    if hair_type:      profile_lines.append(f"  • Hair type:      {hair_type}")
    if current_phase:  profile_lines.append(f"  • Current phase:  {current_phase.replace('_', ' ')}")
    if skin_concerns:  profile_lines.append(f"  • Skin concerns:  {', '.join(skin_concerns)}")
    if hair_concerns:  profile_lines.append(f"  • Hair concerns:  {', '.join(hair_concerns)}")
    if allergies:      profile_lines.append(f"  • Allergies:      {', '.join(allergies)}")

    if not profile_lines:
        return base + "\nNo detailed profile data available — give general advice."

    profile_block = "\n".join(profile_lines)
    return base + f"""

── USER SKIN PROFILE (always use this as context) ──────────────────
{profile_block}
────────────────────────────────────────────────────────────────────
Always tailor your advice to this specific profile. When recommending \
products or routines, account for their skin type, concerns, and allergies.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user_profile(user_id: str) -> dict | None:
    """Load the user document from MongoDB."""
    db = get_db()
    try:
        doc = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        doc = await db.users.find_one({"_id": user_id})
    return doc


async def _load_history(user_id: str, limit: int = 20) -> list[dict]:
    """
    Return the last `limit` messages for this user, oldest first.
    These are passed directly to Claude as the conversation history.
    """
    db = get_db()
    cursor = (
        db.chat_messages
        .find({"user_id": user_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    docs.reverse()  # oldest first for Claude
    return [{"role": d["role"], "content": d["content"]} for d in docs]


async def _save_message(user_id: str, role: str, content: str) -> None:
    """Persist a single message to MongoDB."""
    db = get_db()
    await db.chat_messages.insert_one({
        "user_id":    user_id,
        "role":       role,
        "content":    content,
        "created_at": datetime.now(timezone.utc),
    })


# ── SSE stream generator ──────────────────────────────────────────────────────

async def _stream_reply(
    user_id:  str,
    messages: list[dict],
    system:   str,
) -> AsyncGenerator[str, None]:
    """
    Streams Claude's reply token by token as Server-Sent Events.

    Each chunk:   data: {"chunk": "..."}\n\n
    End signal:   data: [DONE]\n\n

    After the stream ends, the full reply is saved to MongoDB.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        yield 'data: {"error": "ANTHROPIC_API_KEY not configured"}\n\n'
        return

    client      = anthropic.AsyncAnthropic(api_key=api_key)
    full_reply  = []

    try:
        async with client.messages.stream(
            model      = "claude-sonnet-4-20250514",
            max_tokens = 1024,
            system     = system,
            messages   = messages,
        ) as stream:
            async for text_chunk in stream.text_stream:
                full_reply.append(text_chunk)
                payload = json.dumps({"chunk": text_chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

    except anthropic.AuthenticationError:
        yield 'data: {"error": "Invalid API key."}\n\n'
        return
    except anthropic.RateLimitError:
        yield 'data: {"error": "Rate limit hit. Please try again shortly."}\n\n'
        return
    except anthropic.APITimeoutError:
        yield 'data: {"error": "Request timed out. Please try again."}\n\n'
        return
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", exc)
        yield f'data: {{"error": "API error: {exc}"}}\n\n'
        return

    # ── Save assistant reply after stream completes ───────────────────────────
    if full_reply:
        await _save_message(user_id, "assistant", "".join(full_reply))

    yield "data: [DONE]\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/message",
    summary     = "Send a chat message (SSE streaming)",
    description = (
        "Send a message to SkinSense AI. The reply streams back as Server-Sent Events.\n\n"
        "**SSE format:**\n"
        "```\n"
        "data: {\"chunk\": \"Hello\"}\n"
        "data: {\"chunk\": \" there\"}\n"
        "data: [DONE]\n"
        "```\n\n"
        "The user's skin profile is automatically injected as context. "
        "Chat history (last 20 messages) is included for continuity."
    ),
    responses={
        200: {"description": "SSE stream of reply chunks"},
        401: {"description": "Invalid or expired JWT"},
    },
)
async def send_message(
    payload: ChatMessageRequest,
    user_id: str = Depends(_get_current_user_id),
):
    user_text = payload.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    # 1 — Save the user's message immediately
    await _save_message(user_id, "user", user_text)

    # 2 — Load context: profile + history
    profile     = await _get_user_profile(user_id)
    system      = _build_system_prompt(profile)
    history     = await _load_history(user_id, limit=20)  # includes the message we just saved

    # 3 — Stream Claude reply
    return StreamingResponse(
        _stream_reply(user_id, history, system),
        media_type = "text/event-stream",
        headers    = {
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",   # disable nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get(
    "/history",
    response_model = ChatHistoryResponse,
    summary        = "Get chat history",
    description    = "Returns the last N messages for the authenticated user (default 50).",
)
async def get_history(
    limit:   int = Query(default=50, ge=1, le=200, description="Number of messages to return"),
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


@router.delete(
    "/history",
    summary     = "Clear chat history",
    description = "Permanently deletes all chat messages for the authenticated user.",
)
async def clear_history(user_id: str = Depends(_get_current_user_id)):
    db     = get_db()
    result = await db.chat_messages.delete_many({"user_id": user_id})
    return {"success": True, "deleted": result.deleted_count}