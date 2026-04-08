# routers/scan_product.py
"""
POST /scan/product

Accepts 1–4 images of a cosmetic product, detects barcodes/QR codes locally,
enriches the context from Open Beauty Facts, then sends everything to Claude
Vision for a comprehensive structured product analysis.

Pipeline:
  1. Validate images (magic bytes, size, blur)
  2. Decode any barcode / QR code found in the images  (pyzbar — optional)
  3. Lookup barcode on Open Beauty Facts API           (httpx async)
  4. Send image(s) + enrichment data to Claude Vision
  5. Return structured product JSON

Response shape:
  {
    "product_detected": true,
    "product_name":     str,
    "brand":            str | null,
    "weight":           str | null,       # e.g. "150 ml", "50 g"
    "best_use":         str,              # skin type / use case
    "how_to_apply":     str,              # step-by-step usage
    "side_effects":     str,              # warnings / known reactions
    "ingredients":      [str],            # key active ingredients
    "barcode":          str | null,       # detected barcode value
    "data_source":      str,              # "vision_only" | "barcode_enriched"
    "confidence":       "high"|"medium"|"low"
  }

Error body shape:
  { "code": "NO_PRODUCT" | "BLURRY_IMAGE" | ..., "detail": str }

Optional dependency:
  pip install pyzbar          # barcode decoding (requires libzbar0 on Linux)
  apt-get install libzbar0    # system library for pyzbar

  If pyzbar is not installed, barcode decoding is silently skipped and Claude
  still analyses the product visually — the endpoint works either way.

Mock mode (MOCK_MODE=true in .env): hardcoded response, no API/barcode calls.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from typing import Any
from typing import List

import anthropic
import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scan", tags=["Scan"])

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_IMAGES       = 4
MAX_FILE_SIZE_MB = 10
MAX_FILE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024

# Products are usually shot in normal room light — standard blur threshold
BLUR_THRESHOLD = 80.0

RESIZE_MAX_PX = 1024
JPEG_QUALITY  = 85   # slightly higher than skin scans for label readability

# Open Beauty Facts — free, no API key required
# Falls back to Open Food Facts for non-beauty barcodes
OPEN_BEAUTY_FACTS_URL  = "https://world.openbeautyfacts.org/api/v2/product/{}.json"
OPEN_FOOD_FACTS_URL    = "https://world.openfoodfacts.org/api/v2/product/{}.json"
BARCODE_LOOKUP_TIMEOUT = 6.0   # seconds

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

class ProductScanResponse(BaseModel):
    product_detected: bool
    product_name:     str
    brand:            str | None = None
    weight:           str | None = None
    best_use:         str
    how_to_apply:     str
    side_effects:     str
    ingredients:      List[str]  = []
    barcode:          str | None = None
    data_source:      str        = "vision_only"   # vision_only | barcode_enriched
    confidence:       str        = "medium"        # high | medium | low

# ── Mock response ─────────────────────────────────────────────────────────────

MOCK_RESPONSE = ProductScanResponse(
    product_detected = True,
    product_name     = "CeraVe Moisturising Cream",
    brand            = "CeraVe",
    weight           = "340 g",
    best_use         = (
        "Best for dry to very dry skin on face and body. "
        "Suitable for sensitive skin and eczema-prone skin."
    ),
    how_to_apply     = (
        "1. Cleanse skin thoroughly.\n"
        "2. Apply a generous amount to affected areas.\n"
        "3. Massage gently until fully absorbed.\n"
        "4. Use morning and night, or as directed by a dermatologist."
    ),
    side_effects     = (
        "Generally well tolerated. In rare cases may cause mild irritation or "
        "allergic reaction. Discontinue use if redness or itching occurs. "
        "Avoid contact with eyes."
    ),
    ingredients      = [
        "Ceramides (1, 3, 6-II)",
        "Hyaluronic Acid",
        "Niacinamide",
        "Petrolatum",
        "Dimethicone",
    ],
    barcode          = "301871239015",
    data_source      = "barcode_enriched",
    confidence       = "high",
)

# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(barcode_data: dict | None) -> str:
    """
    Build the system prompt, optionally injecting enrichment data
    retrieved from a barcode/QR code lookup.
    """
    enrichment_block = ""
    if barcode_data:
        enrichment_block = f"""
ENRICHMENT DATA (retrieved from barcode database — treat as ground truth):
{json.dumps(barcode_data, indent=2, ensure_ascii=False)}

Use the enrichment data above as your primary source of truth.
Fill any gaps using what you can read visually from the product images.
"""

    return f"""
You are an expert cosmetic product analyst with deep knowledge of beauty,
skincare, and personal care products worldwide.

You will receive 1–4 photos of a cosmetic/personal-care product.
{enrichment_block}
STEP 1 — PRODUCT VALIDATION
Check every image:
  • Does it clearly show a cosmetic or personal-care product?
  • Is the product packaging, label, or bottle the main subject?
  • Reject: face selfies, food, non-cosmetic objects, blank/empty images.

If validation fails, return ONLY:
{{
  "product_detected": false,
  "error_reason": "<one of: no_product | not_cosmetic | image_unclear | other>",
  "error_detail": "<one short sentence>"
}}

STEP 2 — PRODUCT ANALYSIS (only if validation passes)
Analyse the product thoroughly and return ONLY this JSON:
{{
  "product_detected": true,
  "product_name":  "<full product name as printed on packaging>",
  "brand":         "<brand name, or null if unreadable>",
  "weight":        "<weight or volume with unit e.g. '150 ml' '50 g', or null>",
  "best_use":      "<2-3 sentences: skin/hair type it suits, primary use case, who it is for>",
  "how_to_apply":  "<numbered step-by-step application instructions, be specific>",
  "side_effects":  "<known warnings, allergens, contraindications, or 'No significant side effects reported' if none>",
  "ingredients":   ["<top 5-8 key active ingredients only>"],
  "barcode":       "<barcode or QR code value visible in image, or null>",
  "confidence":    "<high|medium|low based on how clearly the product is identifiable>"
}}

Confidence guide:
  high   — product name, brand, and key details are clearly legible
  medium — product is identifiable but some details are partially obscured
  low    — product is guessed from partial label or shape/colour only

Rules:
  - Return ONLY valid JSON — no markdown, no code fences, no extra keys.
  - Be as specific and accurate as possible — users rely on this for safety.
  - For side_effects: always mention if the product contains common allergens
    (fragrance, parabens, sulphates, alcohol, essential oils, retinol, AHA/BHA).
  - ingredients list should contain ingredient names only, no amounts.
  - If enrichment data is provided, prefer it over visual guessing for
    ingredients, weight, and exact product name.
""".strip()

# ── Error helper ──────────────────────────────────────────────────────────────

def _err(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code = status,
        detail      = {"code": code, "detail": detail},
    )

# ── Blur detection (numpy-based — fast & accurate) ────────────────────────────

def _laplacian_variance(image_bytes: bytes) -> float:
    """
    Compute Laplacian variance to detect blurry images.
    Uses numpy if available (fast), falls back to pure-Python (slow but correct).
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("L")

    try:
        import numpy as np  # type: ignore
        arr  = np.array(img, dtype=np.float32)
        lap  = (
            np.roll(arr, -1, axis=0) + np.roll(arr, 1, axis=0)
            + np.roll(arr, -1, axis=1) + np.roll(arr, 1, axis=1)
            - 4 * arr
        )
        return float(np.var(lap))
    except ImportError:
        # Pure-Python fallback (samples every 4th pixel for speed)
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
        return sum((v - mean) ** 2 for v in lap_vals) / n

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

# ── Barcode / QR detection ────────────────────────────────────────────────────

def _decode_barcodes(image_bytes: bytes) -> list[str]:
    """
    Attempt to decode all barcodes and QR codes in the image using pyzbar.
    Returns a list of decoded string values (may be empty).

    pyzbar is optional — if not installed this function returns [] silently.
    Requires system library: apt-get install libzbar0
    """
    try:
        # FIX: wrap entire block so any ImportError (e.g. missing libzbar0 / cv2)
        # is caught gracefully — not just the top-level pyzbar import.
        from pyzbar import pyzbar  # type: ignore
        img     = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        decoded = pyzbar.decode(img)
        values  = [d.data.decode("utf-8", errors="ignore") for d in decoded if d.data]
        if values:
            logger.info("Barcodes detected locally: %s", values)
        return values
    except ImportError:
        logger.debug("pyzbar not installed — skipping local barcode decode.")
        return []
    except Exception as exc:
        logger.warning("Barcode decode error: %s", exc)
        return []

# ── Open Beauty Facts lookup ──────────────────────────────────────────────────

async def _fetch_barcode_data(barcode: str) -> dict | None:
    """
    Query Open Beauty Facts (then Open Food Facts as fallback) for product data.
    Returns a cleaned dict of useful fields, or None on failure.
    """
    async with httpx.AsyncClient(timeout=BARCODE_LOOKUP_TIMEOUT) as client:
        for url_template in (OPEN_BEAUTY_FACTS_URL, OPEN_FOOD_FACTS_URL):
            url = url_template.format(barcode)
            try:
                resp = await client.get(url, headers={"User-Agent": "SkinSense/1.0"})
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if body.get("status") != 1:
                    continue

                p = body.get("product", {})

                # Extract the most useful fields for the prompt
                data: dict[str, Any] = {}

                for key in ("product_name", "brands", "quantity",
                            "categories", "countries"):
                    val = p.get(key, "")
                    if val:
                        data[key] = val

                # Ingredients
                ing_text = p.get("ingredients_text_en") or p.get("ingredients_text", "")
                if ing_text:
                    data["ingredients_text"] = ing_text

                ing_list = p.get("ingredients", [])
                if ing_list:
                    data["ingredients_list"] = [
                        i.get("text", "") for i in ing_list[:20] if i.get("text")
                    ]

                # Warnings / labels
                for key in ("warnings", "conservation_conditions",
                            "periods_after_opening", "allergens",
                            "traces", "nutriments"):
                    val = p.get(key)
                    if val:
                        data[key] = val

                # Image URLs (for reference only — not sent to Claude)
                if p.get("image_url"):
                    data["official_image_url"] = p["image_url"]

                logger.info("Barcode %s enriched from %s", barcode, url_template)
                return data if data else None

            except httpx.TimeoutException:
                logger.warning("Barcode lookup timed out for %s at %s", barcode, url_template)
            except Exception as exc:
                logger.warning("Barcode lookup error for %s: %s", barcode, exc)

    return None

# ── Per-file validation ───────────────────────────────────────────────────────

async def _validate_and_prepare(
    file: UploadFile,
    idx: int,
) -> tuple[bytes, str]:
    """
    Validates and returns (raw_bytes, media_type).
    Raw bytes are kept (not yet base64) so barcode detection can run on them.
    """
    label = file.filename or f"image_{idx}"
    data  = await file.read()

    if len(data) == 0:
        raise _err(400, "EMPTY_FILE", f"'{label}' is empty.")

    if len(data) > MAX_FILE_BYTES:
        raise _err(
            400, "FILE_TOO_LARGE",
            f"'{label}' is {len(data)/1024/1024:.1f} MB — max is {MAX_FILE_SIZE_MB} MB.",
        )

    media_type = _sniff_media_type(data)
    if media_type is None:
        raise _err(
            400, "UNSUPPORTED_FORMAT",
            f"'{label}' is not a recognised image (jpeg/png/webp/gif).",
        )

    variance = await asyncio.to_thread(_laplacian_variance, data)
    logger.debug("'%s' Laplacian variance = %.2f", label, variance)

    if variance < BLUR_THRESHOLD:
        raise _err(
            400, "BLURRY_IMAGE",
            f"'{label}' is too blurry (sharpness {variance:.0f} < {BLUR_THRESHOLD:.0f}). "
            "Please retake the photo in good lighting — the product label must be sharp "
            "and fully readable.",
        )

    return data, media_type

# ── Content blocks ────────────────────────────────────────────────────────────

def _build_content_blocks(encoded: list[tuple[str, str]]) -> list[dict]:
    blocks: list[dict] = []
    for i, (b64, media_type) in enumerate(encoded, start=1):
        blocks.append({"type": "text", "text": f"Product image {i} of {len(encoded)}:"})
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    blocks.append({
        "type": "text",
        "text": (
            "Identify this cosmetic product as accurately as possible. "
            "Read every visible label, text, and barcode in the images. "
            "Return the JSON result following the schema in the system prompt exactly."
        ),
    })
    return blocks

# ── Async Claude call ─────────────────────────────────────────────────────────

async def _call_claude_async(
    encoded: list[tuple[str, str]],
    barcode_data: dict | None,
    detected_barcode: str | None,
) -> ProductScanResponse:

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise _err(500, "MISSING_API_KEY", "Server misconfiguration: ANTHROPIC_API_KEY not set.")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        message = await client.messages.create(
            model      = "claude-sonnet-4-5",   # FIX: corrected from invalid "claude-sonnet-4-20250514"
            max_tokens = 2048,
            system     = _build_system_prompt(barcode_data),
            messages   = [{"role": "user", "content": _build_content_blocks(encoded)}],
        )
    except anthropic.AuthenticationError:
        raise _err(500, "INVALID_API_KEY", "Invalid ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        raise _err(429, "RATE_LIMITED", "Anthropic rate limit hit. Retry in a few seconds.")
    except anthropic.APITimeoutError:
        raise _err(504, "TIMEOUT", "Anthropic API timed out. Please retry.")
    except anthropic.APIConnectionError:
        raise _err(502, "API_UNREACHABLE", "Could not reach the Anthropic API.")
    except anthropic.APIStatusError as exc:
        raise _err(502, "API_ERROR", f"Anthropic error {exc.status_code}: {exc.message}")
    except anthropic.APIError as exc:
        raise _err(502, "API_ERROR", str(exc))

    if not message.content:
        raise _err(502, "EMPTY_RESPONSE", "Anthropic returned an empty response.")

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude non-JSON: %s", raw[:400])
        raise _err(422, "MALFORMED_RESPONSE", f"Claude returned malformed JSON: {raw[:200]}")

    # ── Product not detected ──────────────────────────────────────────────────
    if not data.get("product_detected", True):
        reason = data.get("error_reason", "unknown")
        detail = data.get("error_detail", "No cosmetic product was detected in the image.")
        code_map = {
            "no_product":    "NO_PRODUCT",
            "not_cosmetic":  "NOT_COSMETIC",
            "image_unclear": "IMAGE_UNCLEAR",
        }
        raise _err(400, code_map.get(reason, "NO_PRODUCT"), detail)

    # ── Validate required fields ──────────────────────────────────────────────
    missing = [k for k in ("product_name", "best_use", "how_to_apply", "side_effects")
               if not data.get(k)]
    if missing:
        raise _err(422, "MISSING_FIELDS", f"Claude response missing fields: {missing}")

    # ── Normalise confidence ──────────────────────────────────────────────────
    confidence = str(data.get("confidence", "medium")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    # If we enriched from barcode, bump confidence to at least medium
    if barcode_data and confidence == "low":
        confidence = "medium"

    # ── Resolve barcode: prefer locally decoded over Claude's visual read ─────
    final_barcode = detected_barcode or data.get("barcode") or None

    return ProductScanResponse(
        product_detected = True,
        product_name     = str(data.get("product_name", "Unknown Product")).strip(),
        brand            = str(data["brand"]).strip() if data.get("brand") else None,
        weight           = str(data["weight"]).strip() if data.get("weight") else None,
        best_use         = str(data.get("best_use", "")).strip(),
        how_to_apply     = str(data.get("how_to_apply", "")).strip(),
        side_effects     = str(data.get("side_effects", "")).strip(),
        ingredients      = [str(i).strip() for i in data.get("ingredients", []) if i],
        barcode          = final_barcode,
        data_source      = "barcode_enriched" if barcode_data else "vision_only",
        confidence       = confidence,
    )

# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/product",
    response_model = ProductScanResponse,
    summary        = "Cosmetic Product Scan",
    description    = (
        "Upload 1–4 photos of a cosmetic/personal-care product. "
        "The endpoint reads barcodes/QR codes locally, enriches data from "
        "Open Beauty Facts, then uses Claude Vision to return a full product profile.\n\n"
        "**Tips for best results:**\n"
        "- Include one clear photo of the front label\n"
        "- Include one photo of the barcode / ingredients list (back label)\n"
        "- Ensure text on the label is sharp and readable\n\n"
        "**`data_source` field:**\n"
        "- `vision_only` — product identified by Claude reading the label visually\n"
        "- `barcode_enriched` — barcode found and matched to Open Beauty Facts database\n\n"
        "**Error codes** in `detail.code`:\n"
        "- `NO_PRODUCT` — no cosmetic product visible\n"
        "- `NOT_COSMETIC` — image shows a non-cosmetic item\n"
        "- `IMAGE_UNCLEAR` — product is too obscured to identify\n"
        "- `BLURRY_IMAGE` — label unreadable due to blur\n"
        "- `UNSUPPORTED_FORMAT` — not jpeg/png/webp/gif\n"
        "- `FILE_TOO_LARGE` — exceeds 10 MB\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip all external calls."
    ),
    responses = {
        400: {"description": "Validation failed or product not detected"},
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
                                "description": (
                                    "1–4 product photos. "
                                    "Include front label + barcode for best results."
                                ),
                            }
                        },
                    }
                }
            },
            "required": True,
        }
    },
)
async def product_scan(
    images: List[UploadFile] = File(
        ...,
        description="1–4 product photos (jpeg / png / webp / gif, max 10 MB each).",
    ),
) -> ProductScanResponse:

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — returning mock product scan response.")
        return MOCK_RESPONSE

    # ── Count check ───────────────────────────────────────────────────────────
    if not images:
        raise _err(400, "NO_IMAGES", "No images provided. Upload 1–4 product photos.")
    if len(images) > MAX_IMAGES:
        raise _err(400, "TOO_MANY_IMAGES",
                   f"Maximum {MAX_IMAGES} images allowed; you sent {len(images)}.")

    # ── Validate & prepare concurrently ──────────────────────────────────────
    try:
        raw_images: list[tuple[bytes, str]] = list(
            await asyncio.gather(
                *[_validate_and_prepare(f, i) for i, f in enumerate(images, start=1)]
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during image preparation")
        raise _err(500, "INTERNAL_ERROR", f"Unexpected error: {exc}")

    # ── Barcode detection (runs on all images, takes first hit) ──────────────
    detected_barcode: str | None = None
    barcode_data:     dict | None = None

    for raw_bytes, _ in raw_images:
        codes = await asyncio.to_thread(_decode_barcodes, raw_bytes)
        if codes:
            detected_barcode = codes[0]   # use first detected code
            logger.info("Using barcode: %s", detected_barcode)
            break

    # ── Barcode lookup (async, non-blocking) ─────────────────────────────────
    if detected_barcode:
        barcode_data = await _fetch_barcode_data(detected_barcode)
        if barcode_data:
            logger.info("Enrichment data fetched for barcode %s", detected_barcode)
        else:
            logger.info("No enrichment data found for barcode %s", detected_barcode)

    # ── Optimise images for Claude ────────────────────────────────────────────
    encoded: list[tuple[str, str]] = []
    for raw_bytes, media_type in raw_images:
        opt_bytes, opt_type = await asyncio.to_thread(_optimise_image, raw_bytes, media_type)
        b64 = base64.standard_b64encode(opt_bytes).decode("utf-8")
        encoded.append((b64, opt_type))

    # ── Claude Vision ─────────────────────────────────────────────────────────
    return await _call_claude_async(encoded, barcode_data, detected_barcode)