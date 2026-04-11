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

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from database import get_db
# ── Unified LLM client — works with both Anthropic and LM Studio ─────────────
from claude_client import async_stream_chat, USE_LOCAL_LLM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Chatbot"])

# ── Auth ──────────────────────────────────────────────────────────────────────

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


# ── Request / Response schemas ────────────────────────────────────────────────

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


# ── System prompt builder ─────────────────────────────────────────────────────

def _build_system_prompt(profile: dict | None) -> str:
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
    db = get_db()
    try:
        doc = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        doc = await db.users.find_one({"_id": user_id})
    return doc


async def _load_history(user_id: str, limit: int = 20) -> list[dict]:
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


# ── SSE stream generator ──────────────────────────────────────────────────────

async def _stream_reply(
    user_id:  str,
    messages: list[dict],
    system:   str,
) -> AsyncGenerator[str, None]:
    """
    Streams the LLM reply token-by-token as Server-Sent Events.
    Routes to LM Studio or Anthropic via async_stream_chat().

    Each chunk:  data: {"chunk": "..."}\n\n
    End signal:  data: [DONE]\n\n
    """
    full_reply: list[str] = []

    try:
        async for text_chunk in async_stream_chat(system, messages, max_tokens=1024):
            full_reply.append(text_chunk)
            payload = json.dumps({"chunk": text_chunk}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    except Exception as exc:
        # Catch all LLM errors (connection refused, bad API key, rate limit, etc.)
        # and surface them as an SSE error event so the client can react.
        logger.error("LLM stream error (%s): %s",
                     "LM Studio" if USE_LOCAL_LLM else "Anthropic", exc)

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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/message",
    summary     = "Send a chat message (SSE streaming)",
    description = (
        "Send a message to SkinSense AI. The reply streams back as Server-Sent Events.\n\n"
        "**SSE format:**\n"
        "```\n"
        'data: {"chunk": "Hello"}\n'
        'data: {"chunk": " there"}\n'
        "data: [DONE]\n"
        "```\n\n"
        "The user's skin profile is automatically injected as context. "
        "Chat history (last 20 messages) is included for continuity.\n\n"
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

    await _save_message(user_id, "user", user_text)

    profile = await _get_user_profile(user_id)
    system  = _build_system_prompt(profile)
    history = await _load_history(user_id, limit=20)

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