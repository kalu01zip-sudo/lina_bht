# routers/scan_hair_scalp.py
"""
POST /scan/hair_scalp

Accepts 1–4 scalp/hair images (multipart/form-data), validates them locally
(size, format, magic bytes, blur), then analyses with Claude Vision.

Features:
  ✓ Magic-byte content-type sniffing  → correct type even if browser lies
  ✓ Blur detection (Laplacian variance via PIL) → rejected before API call
  ✓ Non-scalp / wrong-subject detection  → Claude validates in the same call
  ✓ Image resizing + JPEG optimisation  → smaller payloads, faster API round-trip
  ✓ Async Anthropic client  → non-blocking FastAPI handler
  ✓ Richer error body  { "code": ..., "detail": ... }  → easy frontend handling
  ✓ Per-image error reporting  → tells caller exactly which file failed

Response shape (success):
  {
    "score": int,                      # [0-100]
    "advice": str,                     # 2 sentences, 17-20 words
    "detected_triggers": [
      {
        "trigger_name": str,
        "trigger_level": "low"|"medium"|"high",
        "cure_advice": str             # 6-7 words
      }
    ]
  }

Error body shape:
  { "code": "NO_SCALP" | "BLURRY_IMAGE" | "BAD_FORMAT" | ..., "detail": str }

Mock mode (MOCK_MODE=true in .env): hardcoded response, no API call.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from typing import List

import anthropic
from fastapi import APIRouter, File, HTTPException, UploadFile
from claude_client import async_vision_call, is_vision_available, USE_LOCAL_LLM
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scan", tags=["Scan"])

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_IMAGES       = 4
MAX_FILE_SIZE_MB = 10
MAX_FILE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024

# Scalp photos are often close-up macro shots — slightly more lenient blur
# threshold than face scans since fine hair strands reduce Laplacian variance.
BLUR_THRESHOLD = 70.0

RESIZE_MAX_PX = 1024
JPEG_QUALITY  = 82

# ── Magic-byte sniffing ───────────────────────────────────────────────────────

def _sniff_media_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None

# ── Response schema ───────────────────────────────────────────────────────────

class DetectedTrigger(BaseModel):
    trigger_name:  str
    trigger_level: str   # low | medium | high
    cure_advice:   str   # 6-7 words

class HairScalpScanResponse(BaseModel):
    score:             int
    advice:            str
    detected_triggers: List[DetectedTrigger]

# ── Mock response ─────────────────────────────────────────────────────────────

MOCK_RESPONSE = HairScalpScanResponse(
    score  = 65,
    advice = (
        "Your scalp shows moderate dandruff and mild oiliness buildup. "
        "Switch to an anti-dandruff shampoo used three times weekly."
    ),
    detected_triggers = [
        DetectedTrigger(
            trigger_name  = "Dandruff",
            trigger_level = "medium",
            cure_advice   = "Use zinc pyrithione shampoo thrice weekly.",
        ),
        DetectedTrigger(
            trigger_name  = "Oily Scalp",
            trigger_level = "medium",
            cure_advice   = "Wash hair every other day, avoid conditioner roots.",
        ),
        DetectedTrigger(
            trigger_name  = "Hair Thinning",
            trigger_level = "low",
            cure_advice   = "Try minoxidil or consult a trichologist.",
        ),
    ],
)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a certified trichologist AI that analyses human scalp and hair photos.

STEP 1 — IMAGE VALIDATION
Before any analysis, check every provided image for ALL of the following:
  • Does the image clearly show a human scalp, hair strands, or hairline?
  • Is the scalp or hair the primary subject? Reject face selfies, full-body
    shots, animals, objects, drawings, or anything unrelated to scalp/hair.
  • If multiple images are provided, do they all appear to be from the
    SAME person's scalp/hair? Compare hair colour, texture, and scalp tone.

If ANY check fails, return ONLY this JSON and nothing else:
{
  "scalp_detected": false,
  "error_reason": "<one of: no_scalp | not_human | wrong_subject | different_people | other>",
  "error_detail": "<one short sentence explaining what was wrong>"
}

STEP 2 — HAIR & SCALP ANALYSIS (only if all images pass)
Analyse visible hair and scalp conditions across all images and return ONLY:
{
  "scalp_detected": true,
  "score": <integer 0-100, overall hair and scalp health>,
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
  90-100 : Healthy, clean scalp with strong, shiny hair
  75-89  : Minor concerns, mostly healthy
  55-74  : Moderate visible issues
  35-54  : Multiple active concerns
  0-34   : Severe scalp or hair distress

Triggers to report (only if clearly visible):
  Dandruff / Flaking, Oily Scalp, Dry Scalp, Hair Thinning / Hair Loss,
  Scalp Redness / Inflammation, Scalp Acne / Folliculitis, Scalp Psoriasis,
  Seborrheic Dermatitis, Product Buildup, Alopecia Patches,
  Split Ends, Brittle / Damaged Hair, Frizz / Dryness,
  Receding Hairline, Scalp Fungal Infection.

Rules:
  - Return ONLY valid JSON — no markdown, no code fences, no extra keys.
  - advice: exactly 2 sentences, 17-20 words total.
  - cure_advice: exactly 6-7 words.
  - trigger_level: exactly one of: low, medium, high.
  - Only report triggers that are clearly visible in the images.
""".strip()

# ── Error helper ──────────────────────────────────────────────────────────────

def _err(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code = status,
        detail      = {"code": code, "detail": detail},
    )

# ── Blur detection ────────────────────────────────────────────────────────────

def _laplacian_variance(image_bytes: bytes) -> float:
    img    = Image.open(io.BytesIO(image_bytes)).convert("L")
    w, h   = img.size
    pixels = list(img.getdata())

    def px(x: int, y: int) -> int:
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        return pixels[y * w + x]

    lap_vals: list[float] = []
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

# ── Image optimisation ────────────────────────────────────────────────────────

def _optimise_image(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(image_bytes))

    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    max_dim = max(img.width, img.height)
    if max_dim > RESIZE_MAX_PX:
        scale = RESIZE_MAX_PX / max_dim
        img   = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )

    if media_type == "image/gif":
        img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"

# ── Per-file validation ───────────────────────────────────────────────────────

async def _validate_and_prepare(file: UploadFile, idx: int) -> tuple[str, str]:
    label = file.filename or f"image_{idx}"
    data  = await file.read()

    if len(data) == 0:
        raise _err(400, "EMPTY_FILE", f"'{label}' is empty.")

    if len(data) > MAX_FILE_BYTES:
        raise _err(
            400, "FILE_TOO_LARGE",
            f"'{label}' is {len(data)/1024/1024:.1f} MB — max allowed is {MAX_FILE_SIZE_MB} MB.",
        )

    media_type = _sniff_media_type(data)
    if media_type is None:
        raise _err(
            400, "UNSUPPORTED_FORMAT",
            f"'{label}' is not a recognised image (jpeg/png/webp/gif). "
            "Make sure the file is a valid image, not a renamed document.",
        )

    variance = await asyncio.to_thread(_laplacian_variance, data)
    logger.debug("'%s' Laplacian variance = %.2f", label, variance)

    if variance < BLUR_THRESHOLD:
        raise _err(
            400, "BLURRY_IMAGE",
            f"'{label}' is too blurry (sharpness score {variance:.0f} < {BLUR_THRESHOLD:.0f}). "
            "Please retake the photo in good lighting, hold the camera steady, "
            "and ensure the scalp is in sharp focus.",
        )

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
        blocks.append({"type": "text", "text": f"Scalp/hair image {i} of {len(encoded)}:"})
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    blocks.append({
        "type": "text",
        "text": (
            "First validate that every image clearly shows a human scalp or hair "
            "and that all images appear to be from the same person. "
            "Then perform the full hair and scalp analysis. "
            "Return the JSON result following the schema in the system prompt exactly."
        ),
    })
    return blocks

# ── Async vision call ─────────────────────────────────────────────────────────

async def _call_claude_async(encoded: list[tuple[str, str]]) -> HairScalpScanResponse:
    """
    Calls the vision backend (Anthropic or LM Studio VL) asynchronously.
    Routes through async_vision_call() from claude_client.
    """
    try:
        raw = await async_vision_call(
            SYSTEM_PROMPT, _build_content_blocks(encoded), max_tokens=1024
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Vision API error: %s", exc)
        backend = "LM Studio" if USE_LOCAL_LLM else "Anthropic"
        hint    = " Is LM Studio running with your VL model loaded?" if USE_LOCAL_LLM else ""
        raise _err(502, "API_ERROR", f"{backend} error: {exc}.{hint}")

    if not raw:
        raise _err(422, "EMPTY_RESPONSE", "Vision API returned an empty response.")

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

    # ── Check scalp_detected flag ─────────────────────────────────────────────
    if not data.get("scalp_detected", True):
        reason = data.get("error_reason", "unknown")
        detail = data.get("error_detail", "No human scalp or hair was detected in the provided image(s).")

        code_map = {
            "no_scalp":        "NO_SCALP",
            "not_human":       "NOT_HUMAN",
            "wrong_subject":   "WRONG_SUBJECT",
            "different_people":"DIFFERENT_PEOPLE",
        }
        code = code_map.get(reason, "NO_SCALP")
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
            cure_advice   = str(t.get("cure_advice",   "Consult a trichologist.")).strip(),
        ))

    return HairScalpScanResponse(
        score             = score,
        advice            = str(data["advice"]).strip(),
        detected_triggers = triggers,
    )

# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/hair_scalp",
    response_model = HairScalpScanResponse,
    summary        = "Hair & Scalp Scan",
    description    = (
        "Upload 1–4 scalp or hair photos. The endpoint validates each image locally "
        "(format, size, blur), then uses Claude Vision to detect hair and scalp conditions.\n\n"
        "**Tips for best results:**\n"
        "- Part your hair to expose the scalp directly under good lighting\n"
        "- Take close-up shots from different angles (top, sides, hairline)\n"
        "- Avoid flash glare directly on the scalp\n\n"
        "**Error codes** returned in `detail.code`:\n"
        "- `NO_SCALP` — no scalp or hair visible in image\n"
        "- `NOT_HUMAN` — image does not show a human\n"
        "- `WRONG_SUBJECT` — image shows something unrelated (face selfie, object, etc.)\n"
        "- `DIFFERENT_PEOPLE` — uploaded images appear to be from different people\n"
        "- `BLURRY_IMAGE` — image is too blurry for analysis\n"
        "- `UNSUPPORTED_FORMAT` — not jpeg/png/webp/gif\n"
        "- `FILE_TOO_LARGE` — exceeds 10 MB\n"
        "- `RATE_LIMITED` — retry after a few seconds\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip the API call during development."
    ),
    responses = {
        400: {"description": "Image validation failed (format / size / blur / no scalp)"},
        422: {"description": "Claude returned malformed data"},
        429: {"description": "Anthropic rate limit hit"},
        500: {"description": "Server misconfiguration"},
        502: {"description": "Anthropic API error"},
        504: {"description": "Anthropic API timeout"},
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["images"],
                        "properties": {
                            "images": {
                                "type":  "array",
                                "items": {"type": "string", "format": "binary"},
                                "minItems": 1,
                                "maxItems": 4,
                                "description": "1–4 scalp/hair photos (jpeg / png / webp / gif, max 10 MB each).",
                            }
                        },
                    }
                }
            },
            "required": True,
        }
    },
)
async def hair_scalp_scan(
    images: List[UploadFile] = File(
        ...,
        description="1–4 scalp/hair photos (jpeg / png / webp / gif, max 10 MB each).",
    ),
) -> HairScalpScanResponse:

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true" or not is_vision_available():
        logger.info("MOCK_MODE or USE_LOCAL_LLM — returning mock hair/scalp scan response.")
        return MOCK_RESPONSE

    # ── Count check ───────────────────────────────────────────────────────────
    if not images:
        raise _err(400, "NO_IMAGES", "No images provided. Upload 1–4 scalp or hair photos.")

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
        raise
    except Exception as exc:
        logger.exception("Unexpected error during image preparation")
        raise _err(500, "INTERNAL_ERROR", f"Unexpected error: {exc}")

    # ── Claude Vision ─────────────────────────────────────────────────────────
    return await _call_claude_async(encoded)