# routers/scan_face.py
"""
POST /scan/face

Accepts 1–4 face images (multipart/form-data), validates them locally
(size, format, magic bytes, blur), then analyses with Claude Vision.

Improvements over v1:
  ✓ Magic-byte content-type sniffing  → correct type even if browser lies
  ✓ Blur detection (Laplacian variance via PIL) → rejected before API call
  ✓ Non-face / non-human detection  → Claude validates in the same call
  ✓ Image resizing + JPEG optimisation  → smaller payloads, faster API round-trip
  ✓ Async Anthropic client  → non-blocking FastAPI handler
  ✓ Richer error body  { "code": ..., "detail": ... }  → easy frontend handling
  ✓ Per-image error reporting  → tells caller exactly which file failed

Response shape (success):
  {
    "score": int,                      # [0-100]
    "advice": str,                     # 2-line, 17-20 words
    "detected_triggers": [
      {
        "trigger_name": str,
        "trigger_level": "low"|"medium"|"high",
        "cure_advice": str             # 6-7 words
      }
    ]
  }

Error body shape:
  { "code": "NO_FACE" | "BLURRY" | "BAD_FORMAT" | ..., "detail": str }

Mock mode (MOCK_MODE=true in .env): hardcoded response, no API call.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import struct
from typing import List

import anthropic
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scan", tags=["Scan"])

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_IMAGES        = 4
MAX_FILE_SIZE_MB  = 10
MAX_FILE_BYTES    = MAX_FILE_SIZE_MB * 1024 * 1024

# Laplacian variance below this → blurry (tune as needed, 80–120 is typical)
BLUR_THRESHOLD = 90.0

# Images are resized to this max dimension before sending to Claude.
# Keeps payloads small without losing diagnostic detail.
RESIZE_MAX_PX  = 1024
JPEG_QUALITY   = 82          # 80-85 is a good quality/size trade-off

# ── Supported types & magic-byte table ───────────────────────────────────────

# Maps *detected* media type → Anthropic-accepted media type
SUPPORTED_MEDIA_TYPES: dict[str, str] = {
    "image/jpeg": "image/jpeg",
    "image/png":  "image/png",
    "image/gif":  "image/gif",
    "image/webp": "image/webp",
}

def _sniff_media_type(data: bytes) -> str | None:
    """
    Detect image type from magic bytes.
    Returns an Anthropic-compatible media-type string, or None if unrecognised.
    This is more reliable than trusting Content-Type headers from clients.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # RIFF....WEBP
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None

# ── Response schema ───────────────────────────────────────────────────────────

class DetectedTrigger(BaseModel):
    trigger_name:  str
    trigger_level: str   # low | medium | high
    cure_advice:   str   # 6-7 words

class FaceScanResponse(BaseModel):
    score:             int
    advice:            str
    detected_triggers: List[DetectedTrigger]

# ── Mock response ─────────────────────────────────────────────────────────────

MOCK_RESPONSE = FaceScanResponse(
    score  = 72,
    advice = (
        "Your skin shows mild dehydration and early acne signs. "
        "Stick to a gentle routine with SPF every morning."
    ),
    detected_triggers = [
        DetectedTrigger(
            trigger_name  = "Acne / Pimples",
            trigger_level = "medium",
            cure_advice   = "Use salicylic acid cleanser twice daily.",
        ),
        DetectedTrigger(
            trigger_name  = "Dehydration",
            trigger_level = "medium",
            cure_advice   = "Apply hyaluronic acid serum on damp skin.",
        ),
        DetectedTrigger(
            trigger_name  = "Mild Redness",
            trigger_level = "low",
            cure_advice   = "Try a calming centella or niacinamide toner.",
        ),
    ],
)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a certified dermatologist AI that analyses human face photos.

STEP 1 — FACE VALIDATION
Before any analysis, check every provided image for ALL of the following:
  • Does it contain a clearly visible human face?
  • Is the face the primary subject (not a crowd, drawing, animal, object)?
  • If multiple images are provided, do they ALL show the SAME person?
    Compare facial bone structure, eye shape, nose, skin tone, and distinctive
    features across every image. If you are not confident they are the same
    person, treat it as a failure.

If ANY check fails, return ONLY this JSON and nothing else:
{
  "face_detected": false,
  "error_reason": "<one of: no_face | non_human | drawing_or_photo_of_photo | too_many_faces_unclear | different_people | other>",
  "error_detail": "<one short sentence explaining what was wrong>"
}

STEP 2 — SKIN ANALYSIS (only if all images pass)
Analyse visible skin conditions across all images and return ONLY this JSON:
{
  "face_detected": true,
  "score": <integer 0-100, overall visible skin health>,
  "advice": "<exactly 2 sentences, total 17-20 words, warm and actionable>",
  "detected_triggers": [
    {
      "trigger_name": "<concise condition name>",
      "trigger_level": "<low|medium|high>",
      "cure_advice": "<exactly 6-7 words, specific actionable tip>"
    }
  ]
}

Scoring guide:
  90-100 : Clear, healthy, even-toned skin
  75-89  : Minor concerns, mostly healthy
  55-74  : Moderate visible issues
  35-54  : Multiple active concerns
  0-34   : Severe skin distress

Triggers to report (only if clearly visible):
  Acne / Pimples, Blackheads, Whiteheads, Oiliness, Dehydration,
  Dark Spots / Hyperpigmentation, Uneven Skin Tone, Redness / Irritation,
  Enlarged Pores, Fine Lines / Wrinkles, Dark Circles, Puffiness,
  Eczema / Dry Patches, Rosacea, Sun Damage, Dullness.

Rules:
  - Return ONLY valid JSON — no markdown, no code fences, no extra keys.
  - advice: exactly 2 sentences, 17-20 words total.
  - cure_advice: exactly 6-7 words.
  - trigger_level: exactly one of: low, medium, high.
""".strip()

# ── Error helper ──────────────────────────────────────────────────────────────

def _err(status: int, code: str, detail: str) -> HTTPException:
    """Raise a structured HTTPException with a { code, detail } body."""
    return HTTPException(
        status_code = status,
        detail      = {"code": code, "detail": detail},
    )

# ── Blur detection ────────────────────────────────────────────────────────────

def _laplacian_variance(image_bytes: bytes) -> float:
    """
    Compute the Laplacian variance of a grayscale image — a standard
    sharpness metric.  Low variance → blurry.
    Runs in a thread to avoid blocking the event loop.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("L")

    # Manual discrete Laplacian (avoids numpy dependency):
    #   kernel = [[0,1,0],[1,-4,1],[0,1,0]]
    w, h    = img.size
    pixels  = list(img.getdata())

    def px(x: int, y: int) -> int:
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        return pixels[y * w + x]

    lap_vals: list[float] = []
    # Sample every 4th pixel for speed on large images
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            val = (px(x, y - 1) + px(x, y + 1)
                   + px(x - 1, y) + px(x + 1, y)
                   - 4 * px(x, y))
            lap_vals.append(float(val))

    n    = len(lap_vals)
    mean = sum(lap_vals) / n
    var  = sum((v - mean) ** 2 for v in lap_vals) / n
    return var

# ── Image preprocessing ───────────────────────────────────────────────────────

def _optimise_image(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """
    Resize the image so its longest side ≤ RESIZE_MAX_PX, then re-encode
    as JPEG (smaller payload, faster upload).  GIFs keep their original type.

    Returns (optimised_bytes, new_media_type).
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Keep EXIF orientation correct
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Resize if needed
    max_dim = max(img.width, img.height)
    if max_dim > RESIZE_MAX_PX:
        scale = RESIZE_MAX_PX / max_dim
        new_w = max(1, int(img.width  * scale))
        new_h = max(1, int(img.height * scale))
        img   = img.resize((new_w, new_h), Image.LANCZOS)

    # GIF → keep as PNG to preserve quality
    if media_type == "image/gif":
        img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"

    # Everything else → JPEG
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"

# ── Per-file validation ───────────────────────────────────────────────────────

async def _validate_and_prepare(file: UploadFile, idx: int) -> tuple[str, str]:
    """
    Full pipeline for a single uploaded file:
      1. Read bytes
      2. Size check
      3. Magic-byte type sniff
      4. Blur detection (runs in thread pool)
      5. Image optimisation (runs in thread pool)

    Returns (base64_data, media_type) ready for the Anthropic payload.
    Raises HTTPException with a structured body on any failure.
    """
    label = file.filename or f"image_{idx}"

    # 1 — Read
    data = await file.read()

    if len(data) == 0:
        raise _err(400, "EMPTY_FILE", f"'{label}' is empty.")

    # 2 — Size
    if len(data) > MAX_FILE_BYTES:
        raise _err(
            400, "FILE_TOO_LARGE",
            f"'{label}' is {len(data)/1024/1024:.1f} MB — max allowed is {MAX_FILE_SIZE_MB} MB.",
        )

    # 3 — Magic bytes (ignore Content-Type header — it can be spoofed)
    media_type = _sniff_media_type(data)
    if media_type is None:
        raise _err(
            400, "UNSUPPORTED_FORMAT",
            f"'{label}' is not a recognised image (jpeg/png/webp/gif). "
            "Make sure the file is a valid image, not a renamed document.",
        )

    # 4 — Blur detection (CPU-bound → off the event loop)
    variance = await asyncio.to_thread(_laplacian_variance, data)
    logger.debug("'%s' Laplacian variance = %.2f", label, variance)

    if variance < BLUR_THRESHOLD:
        raise _err(
            400, "BLURRY_IMAGE",
            f"'{label}' is too blurry (sharpness score {variance:.0f} < {BLUR_THRESHOLD:.0f}). "
            "Please retake the photo in good lighting, keep the camera steady, "
            "and ensure your face is in focus.",
        )

    # 5 — Optimise (CPU-bound → off the event loop)
    optimised, media_type = await asyncio.to_thread(_optimise_image, data, media_type)

    b64 = base64.standard_b64encode(optimised).decode("utf-8")
    logger.info(
        "'%s' prepared: %.1f KB → %.1f KB (%s)",
        label, len(data) / 1024, len(optimised) / 1024, media_type,
    )
    return b64, media_type

# ── Content blocks ────────────────────────────────────────────────────────────

def _build_content_blocks(encoded: list[tuple[str, str]]) -> list[dict]:
    blocks: list[dict] = []
    for i, (b64, media_type) in enumerate(encoded, start=1):
        blocks.append({"type": "text", "text": f"Face image {i} of {len(encoded)}:"})
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    blocks.append({
        "type": "text",
        "text": (
            "First validate that every image contains a clearly visible human face "
            "and that all images show the same person. "
            "Then perform the full skin analysis. "
            "Return the JSON result following the schema in the system prompt exactly."
        ),
    })
    return blocks

# ── Async Claude call ─────────────────────────────────────────────────────────

async def _call_claude_async(encoded: list[tuple[str, str]]) -> FaceScanResponse:
    """
    Calls Claude Vision asynchronously.
    Handles face-not-found responses (returned by the model, not HTTP errors)
    and all Anthropic SDK exceptions.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise _err(500, "MISSING_API_KEY", "Server misconfiguration: ANTHROPIC_API_KEY is not set.")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        message = await client.messages.create(
            model      = "claude-sonnet-4-20250514",
            max_tokens = 1024,
            system     = SYSTEM_PROMPT,
            messages   = [{"role": "user", "content": _build_content_blocks(encoded)}],
        )
    except anthropic.AuthenticationError:
        raise _err(500, "INVALID_API_KEY", "Invalid ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        raise _err(429, "RATE_LIMITED", "Anthropic rate limit hit. Please retry in a few seconds.")
    except anthropic.APITimeoutError:
        raise _err(504, "TIMEOUT", "Anthropic API timed out. Please retry.")
    except anthropic.APIConnectionError:
        raise _err(502, "API_UNREACHABLE", "Could not reach the Anthropic API.")
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic status error %s: %s", exc.status_code, exc.message)
        raise _err(502, "API_ERROR", f"Anthropic returned error {exc.status_code}: {exc.message}")
    except anthropic.APIError as exc:
        logger.error("Unexpected Anthropic error: %s", exc)
        raise _err(502, "API_ERROR", str(exc))

    # ── Parse raw text ────────────────────────────────────────────────────────
    if not message.content:
        raise _err(502, "EMPTY_RESPONSE", "Anthropic returned an empty response.")

    raw = message.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude non-JSON response: %s", raw[:400])
        raise _err(422, "MALFORMED_RESPONSE", f"Claude returned malformed JSON: {raw[:200]}")

    # ── Check face_detected flag ──────────────────────────────────────────────
    if not data.get("face_detected", True):
        reason = data.get("error_reason", "unknown")
        detail = data.get("error_detail", "No human face was detected in the provided image(s).")

        code_map = {
            "no_face":                   "NO_FACE",
            "non_human":                 "NON_HUMAN",
            "drawing_or_photo_of_photo": "NOT_REAL_FACE",
            "too_many_faces_unclear":    "TOO_MANY_FACES",
            "different_people":          "DIFFERENT_PEOPLE",
        }
        code = code_map.get(reason, "NO_FACE")
        raise _err(400, code, detail)

    # ── Validate required fields ──────────────────────────────────────────────
    missing = [k for k in ("score", "advice", "detected_triggers") if k not in data]
    if missing:
        raise _err(422, "MISSING_FIELDS", f"Claude response missing fields: {missing}")

    try:
        score = max(0, min(100, int(data["score"])))
    except (TypeError, ValueError):
        raise _err(422, "INVALID_SCORE", f"Invalid score value: {data.get('score')}")

    # ── Parse triggers ────────────────────────────────────────────────────────
    valid_levels = {"low", "medium", "high"}
    triggers: list[DetectedTrigger] = []

    for i, t in enumerate(data.get("detected_triggers", [])):
        if not isinstance(t, dict):
            continue
        level = str(t.get("trigger_level", "low")).strip().lower()
        if level not in valid_levels:
            level = "low"
        triggers.append(DetectedTrigger(
            trigger_name  = str(t.get("trigger_name",  "Unknown")).strip(),
            trigger_level = level,
            cure_advice   = str(t.get("cure_advice",   "Consult a dermatologist.")).strip(),
        ))

    return FaceScanResponse(
        score             = score,
        advice            = str(data["advice"]).strip(),
        detected_triggers = triggers,
    )

# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/face",
    response_model = FaceScanResponse,
    summary        = "Face Skin Scan",
    description    = (
        "Upload 1–4 face photos. The endpoint validates each image locally "
        "(format, size, blur), then uses Claude Vision to detect skin conditions.\n\n"
        "**Error codes** returned in `detail.code`:\n"
        "- `NO_FACE` / `NON_HUMAN` / `NOT_REAL_FACE` / `TOO_MANY_FACES` — image content issue\n"
        "- `DIFFERENT_PEOPLE` — uploaded images show different people\n"
        "- `BLURRY_IMAGE` — image is too blurry for analysis\n"
        "- `UNSUPPORTED_FORMAT` — not jpeg/png/webp/gif\n"
        "- `FILE_TOO_LARGE` — exceeds 10 MB\n"
        "- `RATE_LIMITED` — retry after a few seconds\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip the API call during development."
    ),
    responses = {
        400: {"description": "Image validation failed (format / size / blur / no face)"},
        422: {"description": "Claude returned malformed data"},
        429: {"description": "Anthropic rate limit hit"},
        500: {"description": "Server misconfiguration"},
        502: {"description": "Anthropic API error"},
        504: {"description": "Anthropic API timeout"},
    },
)
async def face_scan(
    images: List[UploadFile] = File(
        ...,
        description="1–4 face photos (jpeg / png / webp / gif, max 10 MB each).",
    ),
) -> FaceScanResponse:

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — returning mock face scan response.")
        return MOCK_RESPONSE

    # ── Count check ───────────────────────────────────────────────────────────
    if not images:
        raise _err(400, "NO_IMAGES", "No images provided. Upload 1–4 face photos.")

    if len(images) > MAX_IMAGES:
        raise _err(
            400, "TOO_MANY_IMAGES",
            f"Maximum {MAX_IMAGES} images allowed; you sent {len(images)}.",
        )

    # ── Validate & prepare all images concurrently ────────────────────────────
    try:
        encoded: list[tuple[str, str]] = list(
            await asyncio.gather(
                *[_validate_and_prepare(f, i) for i, f in enumerate(images, start=1)]
            )
        )
    except HTTPException:
        raise   # already structured
    except Exception as exc:
        logger.exception("Unexpected error during image preparation")
        raise _err(500, "INTERNAL_ERROR", f"Unexpected error: {exc}")

    # ── Claude Vision ─────────────────────────────────────────────────────────
    return await _call_claude_async(encoded)