"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Claude API Client                           ║
║                                                                  ║
║  Single shared HTTP client for all Claude calls in the app.     ║
║  Handles: vision (image + text) and text-only requests.         ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import base64
import json
import re

import httpx


# ── Constants ───────────────────────────────────────────────────────────────

_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL   = "claude-sonnet-4-20250514"
_VERSION = "2023-06-01"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sniff_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if data[:3]  == b"\xff\xd8\xff":        return "image/jpeg"
    if data[:8]  == b"\x89PNG\r\n\x1a\n":  return "image/png"
    if data[8:12] == b"WEBP":               return "image/webp"
    return "image/jpeg"                      # safe default


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def _extract_text(content: list[dict]) -> str:
    return " ".join(b["text"] for b in content if b.get("type") == "text").strip()


def _parse_json(text: str) -> dict:
    """
    Robustly extract a JSON object from Claude's reply.
    Strips markdown fences, leading/trailing prose.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    match   = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON object found in Claude response:\n{text[:300]}")


# ── Client ───────────────────────────────────────────────────────────────────

class ClaudeClient:
    """
    Thin, synchronous wrapper around the Anthropic Messages API.

    Usage:
        client = ClaudeClient(api_key="sk-ant-...")
        result = client.vision_json(system="...", user="...", image_bytes=b"...")
        note   = client.text(system="...", user="...", max_tokens=64)
    """

    def __init__(self, api_key: str, timeout: int = 60):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required.")
        self._headers = {
            "x-api-key":         api_key,
            "anthropic-version": _VERSION,
            "content-type":      "application/json",
        }
        self._timeout = timeout

    # ── Internal POST ────────────────────────────────────────────────────────

    def _post(self, payload: dict) -> list[dict]:
        with httpx.Client(timeout=self._timeout) as http:
            resp = http.post(_API_URL, json=payload, headers=self._headers)
            resp.raise_for_status()
        return resp.json().get("content", [])

    # ── Public methods ───────────────────────────────────────────────────────

    def vision_json(
        self,
        system:      str,
        user:        str,
        image_bytes: bytes,
        max_tokens:  int = 1024,
    ) -> dict:
        """
        Send an image + text prompt to Claude and return a parsed JSON dict.

        Raises:
            httpx.HTTPStatusError — non-2xx from Anthropic
            ValueError            — response contained no JSON
            json.JSONDecodeError  — malformed JSON in response
        """
        mime = _sniff_mime(image_bytes)
        payload = {
            "model":      _MODEL,
            "max_tokens": max_tokens,
            "system":     system,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": mime,
                            "data":       _b64(image_bytes),
                        },
                    },
                    {"type": "text", "text": user},
                ],
            }],
        }
        content = self._post(payload)
        return _parse_json(_extract_text(content))

    def text(
        self,
        system:     str,
        user:       str,
        max_tokens: int = 128,
    ) -> str:
        """
        Send a text-only prompt and return the raw response string.
        """
        payload = {
            "model":      _MODEL,
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   [{"role": "user", "content": user}],
        }
        content = self._post(payload)
        return _extract_text(content)
