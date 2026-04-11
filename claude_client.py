"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Unified LLM Client                          ║
║                                                                  ║
║  Routes all LLM calls to Anthropic (production) or LM Studio   ║
║  (local dev) based on the USE_LOCAL_LLM env var.                ║
║                                                                  ║
║  Text/chat  → both backends                                     ║
║  Vision     → Anthropic, OR LM Studio if LM_STUDIO_VISION=true ║
║               (requires a VL model e.g. Qwen2.5 VL 7B)         ║
║                                                                  ║
║  .env switches:                                                  ║
║    USE_LOCAL_LLM=true               → use LM Studio             ║
║    LM_STUDIO_BASE_URL=http://...    → LM Studio server URL      ║
║    LM_STUDIO_MODEL=<model name>     → exact name from LM Studio ║
║    LM_STUDIO_VISION=true            → model supports images     ║
║                                       (set for VL models only)  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import AsyncGenerator

import httpx

# ── Runtime config (read once at import time) ────────────────────────────────

USE_LOCAL_LLM = os.environ.get("USE_LOCAL_LLM",    "false").lower() == "true"
_LM_VISION    = os.environ.get("LM_STUDIO_VISION", "false").lower() == "true"

_LM_BASE    = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
_LM_BASE    = _LM_BASE if _LM_BASE.endswith("/v1") else _LM_BASE + "/v1"
_LM_MODEL   = os.environ.get("LM_STUDIO_MODEL",    "local-model")

_ANTH_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
_ANTH_MODEL = "claude-sonnet-4-6"
_ANTH_URL   = "https://api.anthropic.com/v1/messages"
_ANTH_VER   = "2023-06-01"


# ── Public helper: vision availability ───────────────────────────────────────

def is_vision_available() -> bool:
    """
    Returns True when image analysis is available.

    - Anthropic backend  → always True
    - LM Studio backend  → True only when LM_STUDIO_VISION=true
                           (i.e. a VL model like Qwen2.5 VL 7B is loaded)

    Scan endpoints check this; when False they auto-return mock responses.
    """
    if not USE_LOCAL_LLM:
        return True
    return _LM_VISION


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sniff_mime(data: bytes) -> str:
    if data[:3]   == b"\xff\xd8\xff":        return "image/jpeg"
    if data[:8]   == b"\x89PNG\r\n\x1a\n":  return "image/png"
    if data[8:12] == b"WEBP":               return "image/webp"
    return "image/jpeg"

def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()

def _extract_text(content: list[dict]) -> str:
    return " ".join(b["text"] for b in content if b.get("type") == "text").strip()

def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    match   = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON object found in LLM response:\n{text[:300]}")


def _anthropic_blocks_to_openai(blocks: list[dict]) -> list[dict]:
    """
    Convert Anthropic-format content blocks to OpenAI vision format.

    Anthropic:  { "type": "image",
                  "source": { "type": "base64", "media_type": "image/jpeg", "data": "..." } }

    OpenAI:     { "type": "image_url",
                  "image_url": { "url": "data:image/jpeg;base64,..." } }
    """
    result: list[dict] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            result.append({"type": "text", "text": block["text"]})
        elif btype == "image":
            src  = block["source"]
            mime = src.get("media_type", "image/jpeg")
            data = src.get("data", "")
            result.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"}})
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  ClaudeClient
#  Keeps the exact same public interface — profile_scorer.py and
#  claude_vision_analyzer.py need ZERO changes.
# ═══════════════════════════════════════════════════════════════════════════════

class ClaudeClient:
    """
    Drop-in LLM client that routes to Anthropic or LM Studio.

    Public interface (unchanged):
        client = ClaudeClient()
        note   = client.text(system, user, max_tokens=64)
        result = client.vision_json(system, user, image_bytes)
    """

    def __init__(self, api_key: str = "", timeout: int = 60):
        self._api_key = api_key or _ANTH_KEY
        self._timeout = timeout

    def text(self, system: str, user: str, max_tokens: int = 128) -> str:
        if USE_LOCAL_LLM:
            return self._lm_text(system, user, max_tokens)
        return self._anthropic_text(system, user, max_tokens)

    def vision_json(
        self,
        system:      str,
        user:        str,
        image_bytes: bytes,
        max_tokens:  int = 1024,
    ) -> dict:
        if USE_LOCAL_LLM:
            if not _LM_VISION:
                raise RuntimeError(
                    "vision_json() requires LM_STUDIO_VISION=true. "
                    "Load a VL model (e.g. Qwen2.5 VL 7B) and set LM_STUDIO_VISION=true."
                )
            return self._lm_vision_json(system, user, image_bytes, max_tokens)
        return self._anthropic_vision_json(system, user, image_bytes, max_tokens)

    # ── LM Studio ─────────────────────────────────────────────────────────────

    def _lm_text(self, system: str, user: str, max_tokens: int) -> str:
        from openai import OpenAI
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        client = OpenAI(api_key="lm-studio", base_url=_LM_BASE)
        resp   = client.chat.completions.create(
            model=_LM_MODEL, messages=messages, max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()

    def _lm_vision_json(
        self, system: str, user: str, image_bytes: bytes, max_tokens: int
    ) -> dict:
        from openai import OpenAI
        mime = _sniff_mime(image_bytes)
        url  = f"data:{mime};base64,{_b64(image_bytes)}"
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text",      "text": user},
        ]})
        client = OpenAI(api_key="lm-studio", base_url=_LM_BASE)
        resp   = client.chat.completions.create(
            model=_LM_MODEL, messages=messages, max_tokens=max_tokens
        )
        return _parse_json(resp.choices[0].message.content.strip())

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _anthropic_text(self, system: str, user: str, max_tokens: int) -> str:
        payload: dict = {
            "model":      _ANTH_MODEL,
            "max_tokens": max_tokens,
            "messages":   [{"role": "user", "content": user}],
        }
        if system:
            payload["system"] = system
        return _extract_text(self._anthropic_post(payload))

    def _anthropic_vision_json(
        self, system: str, user: str, image_bytes: bytes, max_tokens: int
    ) -> dict:
        mime    = _sniff_mime(image_bytes)
        payload = {
            "model":      _ANTH_MODEL,
            "max_tokens": max_tokens,
            "system":     system,
            "messages": [{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": _b64(image_bytes)}},
                {"type": "text", "text": user},
            ]}],
        }
        return _parse_json(_extract_text(self._anthropic_post(payload)))

    def _anthropic_post(self, payload: dict) -> list[dict]:
        headers = {
            "x-api-key":         self._api_key,
            "anthropic-version": _ANTH_VER,
            "content-type":      "application/json",
        }
        with httpx.Client(timeout=self._timeout) as http:
            resp = http.post(_ANTH_URL, json=payload, headers=headers)
            resp.raise_for_status()
        return resp.json().get("content", [])


# ═══════════════════════════════════════════════════════════════════════════════
#  async_vision_call
#  Used by scan_face, scan_hair_scalp, scan_product.
#  Accepts Anthropic-format content blocks → routes to the right backend.
# ═══════════════════════════════════════════════════════════════════════════════

async def async_vision_call(
    system:         str,
    content_blocks: list[dict],
    max_tokens:     int = 1024,
) -> str:
    """
    Async vision call — returns raw text response.
    Automatically converts Anthropic image blocks → OpenAI format for LM Studio.

    Usage in scan endpoints:
        from claude_client import async_vision_call
        raw  = await async_vision_call(SYSTEM_PROMPT, content_blocks)
        data = json.loads(raw)
    """
    if USE_LOCAL_LLM:
        return await _lm_vision_async(system, content_blocks, max_tokens)
    return await _anthropic_vision_async(system, content_blocks, max_tokens)


async def _anthropic_vision_async(
    system: str, content_blocks: list[dict], max_tokens: int
) -> str:
    import anthropic
    client  = anthropic.AsyncAnthropic(api_key=_ANTH_KEY)
    message = await client.messages.create(
        model=_ANTH_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content_blocks}],
    )
    return message.content[0].text.strip() if message.content else ""


async def _lm_vision_async(
    system: str, content_blocks: list[dict], max_tokens: int
) -> str:
    from openai import AsyncOpenAI
    openai_content = _anthropic_blocks_to_openai(content_blocks)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": openai_content})
    client = AsyncOpenAI(api_key="lm-studio", base_url=_LM_BASE)
    resp   = await client.chat.completions.create(
        model=_LM_MODEL, messages=messages, max_tokens=max_tokens
    )
    return resp.choices[0].message.content.strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  async_stream_chat
#  Used by chat.py for SSE streaming.
# ═══════════════════════════════════════════════════════════════════════════════

async def async_stream_chat(
    system:     str,
    messages:   list[dict],
    max_tokens: int = 1024,
) -> AsyncGenerator[str, None]:
    """Async generator that yields plain text chunks for SSE streaming."""
    if USE_LOCAL_LLM:
        async for chunk in _lm_stream(system, messages, max_tokens):
            yield chunk
    else:
        async for chunk in _anthropic_stream(system, messages, max_tokens):
            yield chunk


async def _anthropic_stream(
    system: str, messages: list[dict], max_tokens: int
) -> AsyncGenerator[str, None]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=_ANTH_KEY)
    async with client.messages.stream(
        model=_ANTH_MODEL, max_tokens=max_tokens, system=system, messages=messages
    ) as stream:
        async for chunk in stream.text_stream:
            yield chunk


async def _lm_stream(
    system: str, messages: list[dict], max_tokens: int
) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI
    full_messages: list[dict] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)
    client = AsyncOpenAI(api_key="lm-studio", base_url=_LM_BASE)
    stream = await client.chat.completions.create(
        model=_LM_MODEL, messages=full_messages, max_tokens=max_tokens, stream=True
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta