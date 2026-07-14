# routers/scan_hair_scalp.py
"""
POST /scan/hair_scalp
GET  /scan/hair_scalp/history

Accepts 1–4 scalp/hair images, validates them locally (size, format, magic
bytes, blur), personalises the Claude system prompt from the user's saved
profile, then analyses with Claude Vision.

Personalisation context injected into every Claude call:
  hair_type, hair_concerns, skin_type (affects scalp oiliness),
  current_phase, allergies, budget

Scan results are saved to MongoDB `scan_results` after every successful call
(including mock) so users can compare hair/scalp health over time.

Response shape (success):
  {
    "scan_id":           str,
    "score":             int,           # 0-100
    "advice":            str,           # 2 sentences, 17-20 words
    "detected_triggers": [
      { "trigger_name": str, "trigger_level": "low|medium|high", "cure_advice": str }
    ]
  }

Error body: { "code": "...", "detail": "..." }
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt as jose_jwt
from PIL import Image
from pydantic import BaseModel

from app.clients.claude_client import USE_LOCAL_LLM, async_vision_call, is_vision_available
from app.core.database import get_db
from app.utils.image_utils import optimise_image

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scan", tags=["Scan"])

# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer()


def _get_current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    secret = os.getenv("SECRET_KEY", "")
    try:
        payload = jose_jwt.decode(creds.credentials, secret, algorithms=["HS256"])
        uid = payload.get("sub") or payload.get("user_id") or payload.get("id")
        if not uid:
            raise ValueError("No user identifier in token")
        return str(uid)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


# ── Constants ─────────────────────────────────────────────────────────────────

MAX_IMAGES       = 4
MAX_FILE_SIZE_MB = 10
MAX_FILE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024

# Scalp photos are close-up macro shots; slightly more lenient than face scans
# because fine hair strands naturally reduce Laplacian variance.
BLUR_THRESHOLD   = 70.0

RESIZE_MAX_PX    = 1024
JPEG_QUALITY     = 82

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


# ── Pydantic models ───────────────────────────────────────────────────────────


class DetectedTrigger(BaseModel):
    trigger_name:  str
    trigger_level: str   # low | medium | high
    cure_advice:   str


class HairScalpScanResponse(BaseModel):
    scan_id:           Optional[str] = None
    score:             int
    advice:            str
    detected_triggers: List[DetectedTrigger]


class ScanHistoryItem(BaseModel):
    scan_id:           str
    score:             int
    advice:            str
    detected_triggers: List[DetectedTrigger]
    scanned_at:        str
    is_mock:           bool
    image_count:       int
    profile_snapshot:  dict


# ── Mock response ─────────────────────────────────────────────────────────────
# Shape is identical to a real Claude response so frontend code works the same
# in MOCK_MODE=true and in production.

MOCK_RESPONSE = HairScalpScanResponse(
    score  = 63,
    advice = (
        "Your scalp shows moderate dandruff and mild oiliness near the roots. "
        "An antifungal shampoo used three times weekly will reduce flaking fast."
    ),
    detected_triggers = [
        DetectedTrigger(
            trigger_name  = "Dandruff / Flaking",
            trigger_level = "medium",
            cure_advice   = "Use ketoconazole shampoo three times weekly.",
        ),
        DetectedTrigger(
            trigger_name  = "Oily Scalp",
            trigger_level = "medium",
            cure_advice   = "Wash every other day, skip conditioner on roots.",
        ),
        DetectedTrigger(
            trigger_name  = "Hair Thinning",
            trigger_level = "low",
            cure_advice   = "Apply minoxidil or consult a trichologist soon.",
        ),
        DetectedTrigger(
            trigger_name  = "Product Buildup",
            trigger_level = "low",
            cure_advice   = "Clarify with a chelating shampoo once monthly.",
        ),
    ],
)

# ── Base system prompt ────────────────────────────────────────────────────────

_BASE_SYSTEM_PROMPT = """
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

# ── Profile personalisation ───────────────────────────────────────────────────

_PHASE_LABELS = {
    "on_my_period": "menstruating — hormonal shifts may worsen scalp oiliness and sensitivity",
    "pregnant":     "pregnant — avoid minoxidil, strong antifungals, and chemical treatments",
    "postpartum":   "postpartum — telogen effluvium (shedding) is very common right now",
    "menopause":    "menopausal — lower oestrogen accelerates hair thinning and scalp dryness",
}

_HAIR_CONCERN_LABELS = {
    "hair_fall":  "Hair Thinning / Hair Loss",
    "dandruff":   "Dandruff / Flaking",
    "oily_scalp": "Oily Scalp",
    "dry_scalp":  "Dry Scalp",
}

_ALLERGEN_LABELS = {
    "fragrance":        "fragrance / perfume",
    "parabens":         "parabens",
    "formaldehyde":     "formaldehyde releasers",
    "phenoxyethanol":   "phenoxyethanol",
    "retinol":          "retinol / retinoids",
    "salicylic_acid":   "salicylic acid",
    "benzoyl_peroxide": "benzoyl peroxide",
    "alcohol_denat":    "denatured alcohol",
    "oxybenzone":       "oxybenzone",
    "nickel":           "nickel",
    "sulfates":         "sulfates (SLS / SLES)",
    "alcohol":          "alcohol",
}

_BUDGET_LABELS = {
    "budget_friendly": "budget-friendly / drugstore",
    "midrange":        "mid-range",
    "premium":         "premium / luxury",
}


async def _get_user_profile(user_id: str) -> dict:
    """
    Fetch the full user document from MongoDB.
    Returns {} silently on any error so the scan still works without personalisation.
    """
    try:
        doc = await get_db()["users"].find_one({"_id": ObjectId(user_id)})
        return doc or {}
    except Exception as exc:
        logger.warning("Could not load profile for user %s: %s", user_id, exc)
        return {}


def _build_profile_snapshot(profile: dict) -> dict:
    return {
        "skin_type":     profile.get("skin_type"),
        "skin_concerns": profile.get("skin_concerns", []),
        "hair_type":     profile.get("hair_type"),
        "hair_concerns": profile.get("hair_concerns", []),
        "current_phase": profile.get("current_phase"),
        "allergies":     profile.get("allergies", []),
        "budget":        profile.get("budget"),
    }


def _build_personalized_system_prompt(profile: dict) -> str:
    """
    Append a USER PROFILE block to the base trichologist prompt.
    Claude uses this to tailor scoring, advice, and product recommendations
    to the user's specific hair type, concerns, and restrictions.
    """
    lines: list[str] = []

    hair_type = profile.get("hair_type")
    if hair_type:
        lines.append(f"  • Hair type: {hair_type.replace('_', ' ')}")

    skin_type = profile.get("skin_type")
    if skin_type:
        lines.append(f"  • Skin type: {skin_type} (oily/dry skin often mirrors scalp behaviour)")

    phase = profile.get("current_phase")
    if phase:
        lines.append(f"  • Hormonal / life phase: {_PHASE_LABELS.get(phase, phase.replace('_', ' '))}")

    hair_concerns = profile.get("hair_concerns", [])
    if hair_concerns:
        readable = [_HAIR_CONCERN_LABELS.get(c, c.replace("_", " ").title()) for c in hair_concerns]
        lines.append(f"  • User-reported hair/scalp concerns: {', '.join(readable)}")

    allergies = profile.get("allergies", [])
    if allergies:
        readable = [_ALLERGEN_LABELS.get(a, a.replace("_", " ")) for a in allergies]
        lines.append(
            f"  • Known allergens — never recommend these in cure_advice: "
            f"{', '.join(readable)}"
        )

    budget = profile.get("budget")
    if budget:
        lines.append(
            f"  • Budget preference: {_BUDGET_LABELS.get(budget, budget)} — "
            f"match product recommendations to this price range."
        )

    if not lines:
        return _BASE_SYSTEM_PROMPT

    return (
        _BASE_SYSTEM_PROMPT
        + "\n\nUSER PROFILE — use this to personalise every part of your response:\n"
        + "\n".join(lines)
        + "\n\nInstructions: weight your score sensitivity to this hair type, "
        "give extra attention to the reported concerns, exclude any allergen from "
        "cure_advice, and match product suggestions to the budget preference."
    )


# ── Error helper ──────────────────────────────────────────────────────────────


def _err(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "detail": detail})


# ── Blur detection ────────────────────────────────────────────────────────────


def _laplacian_variance(image_bytes: bytes) -> float:
    img    = Image.open(io.BytesIO(image_bytes)).convert("L")
    w, h   = img.size
    pixels = list(img.getdata())

    def px(x: int, y: int) -> int:
        return pixels[max(0, min(h-1, y)) * w + max(0, min(w-1, x))]

    lap: list[float] = []
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            lap.append(float(px(x, y-1) + px(x, y+1) + px(x-1, y) + px(x+1, y) - 4*px(x, y)))

    mean = sum(lap) / len(lap)
    return sum((v - mean) ** 2 for v in lap) / len(lap)




# ── Per-file validation ───────────────────────────────────────────────────────


async def _validate_and_prepare(file: UploadFile, idx: int) -> tuple[str, str]:
    label = file.filename or f"image_{idx}"
    data  = await file.read()

    if not data:
        raise _err(400, "EMPTY_FILE", f"'{label}' is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise _err(400, "FILE_TOO_LARGE",
                   f"'{label}' is {len(data)/1024/1024:.1f} MB — max is {MAX_FILE_SIZE_MB} MB.")

    media_type = _sniff_media_type(data)
    if media_type is None:
        raise _err(400, "UNSUPPORTED_FORMAT",
                   f"'{label}' is not a recognised image (jpeg/png/webp/gif).")

    variance = await asyncio.to_thread(_laplacian_variance, data)
    logger.debug("'%s' Laplacian variance = %.2f", label, variance)
    if variance < BLUR_THRESHOLD:
        raise _err(400, "BLURRY_IMAGE",
                   f"'{label}' is too blurry (score {variance:.0f} < {BLUR_THRESHOLD:.0f}). "
                   "Hold camera steady and ensure the scalp is in sharp focus.")

    optimised, media_type = await asyncio.to_thread(
        optimise_image, data, media_type, RESIZE_MAX_PX, JPEG_QUALITY
    )
    b64 = base64.standard_b64encode(optimised).decode()
    logger.info("'%s' %.1f KB → %.1f KB (%s)", label, len(data)/1024, len(optimised)/1024, media_type)
    return b64, media_type


# ── Content blocks ────────────────────────────────────────────────────────────


def _build_content_blocks(encoded: list[tuple[str, str]]) -> list[dict]:
    blocks: list[dict] = []
    for i, (b64, mt) in enumerate(encoded, start=1):
        blocks.append({"type": "text", "text": f"Scalp/hair image {i} of {len(encoded)}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
    blocks.append({
        "type": "text",
        "text": (
            "Validate all images clearly show human scalp or hair and appear to be from "
            "the same person. Then perform the full hair and scalp analysis personalised "
            "to the user profile in the system prompt. Return only the JSON schema above."
        ),
    })
    return blocks


# ── DB save ───────────────────────────────────────────────────────────────────


async def _save_hair_scalp_scan(
    user_id:          str,
    result:           HairScalpScanResponse,
    profile_snapshot: dict,
    image_count:      int,
    is_mock:          bool = False,
) -> str:
    """
    Persist a hair/scalp scan to `scan_results`.

    Fields: user_id, scan_type="hair_scalp", score, advice, detected_triggers,
            scanned_at (UTC), is_mock, image_count, profile_snapshot.
    """
    doc = {
        "user_id":   user_id,
        "scan_type": "hair_scalp",
        "score":     result.score,
        "advice":    result.advice,
        "detected_triggers": [
            {
                "trigger_name":  t.trigger_name,
                "trigger_level": t.trigger_level,
                "cure_advice":   t.cure_advice,
            }
            for t in result.detected_triggers
        ],
        "scanned_at":       datetime.now(timezone.utc),
        "is_mock":          is_mock,
        "image_count":      image_count,
        "profile_snapshot": profile_snapshot,
    }
    res = await get_db()["scan_results"].insert_one(doc)
    return str(res.inserted_id)


# ── Claude Vision call ────────────────────────────────────────────────────────


async def _call_claude_async(
    encoded:       list[tuple[str, str]],
    system_prompt: str,
) -> HairScalpScanResponse:
    try:
        raw = await async_vision_call(system_prompt, _build_content_blocks(encoded), max_tokens=1024)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Vision API error: %s", exc)
        backend = "LM Studio" if USE_LOCAL_LLM else "Anthropic"
        hint    = " Is LM Studio running with your VL model loaded?" if USE_LOCAL_LLM else ""
        raise _err(502, "API_ERROR", f"{backend} error: {exc}.{hint}")

    if not raw:
        raise _err(502, "EMPTY_RESPONSE", "Vision model returned an empty response.")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```")).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Non-JSON Claude response: %s", raw[:400])
        raise _err(422, "MALFORMED_RESPONSE", f"Claude returned malformed JSON: {raw[:200]}")

    if not data.get("scalp_detected", True):
        code_map = {
            "no_scalp":         "NO_SCALP",
            "not_human":        "NOT_HUMAN",
            "wrong_subject":    "WRONG_SUBJECT",
            "different_people": "DIFFERENT_PEOPLE",
        }
        raise _err(
            400,
            code_map.get(data.get("error_reason", ""), "NO_SCALP"),
            data.get("error_detail", "No human scalp or hair detected."),
        )

    missing = [k for k in ("score", "advice", "detected_triggers") if k not in data]
    if missing:
        raise _err(422, "MISSING_FIELDS", f"Claude response missing fields: {missing}")

    try:
        score = max(0, min(100, int(data["score"])))
    except (TypeError, ValueError):
        raise _err(422, "INVALID_SCORE", f"Invalid score: {data.get('score')}")

    valid_levels = {"low", "medium", "high"}
    triggers: list[DetectedTrigger] = []
    for t in data.get("detected_triggers", []):
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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/hair_scalp",
    response_model = HairScalpScanResponse,
    summary        = "Hair & Scalp Scan",
    description    = (
        "Upload 1–4 scalp or hair photos. Images are validated locally (format, size, blur), "
        "then sent to Claude Vision with a **personalised system prompt** built from the "
        "user's hair profile (hair type, concerns, hormonal phase, allergens, budget).\n\n"
        "Results are saved to the database. Use `GET /scan/hair_scalp/history` to compare over time.\n\n"
        "**Tips for best results:**\n"
        "- Part your hair to expose the scalp directly under good lighting\n"
        "- Take close-up shots from different angles (top, sides, hairline)\n"
        "- Avoid flash glare directly on the scalp\n\n"
        "**Error codes** in `detail.code`:\n"
        "- `NO_SCALP` — no scalp or hair visible\n"
        "- `NOT_HUMAN` — image does not show a human\n"
        "- `WRONG_SUBJECT` — face selfie or unrelated image\n"
        "- `DIFFERENT_PEOPLE` — images show different people\n"
        "- `BLURRY_IMAGE` — image too blurry\n"
        "- `UNSUPPORTED_FORMAT` — not jpeg/png/webp/gif\n"
        "- `FILE_TOO_LARGE` — exceeds 10 MB\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip the API call during development."
    ),
    responses={
        400: {"description": "Image validation failed"},
        401: {"description": "Missing or invalid JWT"},
        422: {"description": "Claude returned malformed data"},
        502: {"description": "Vision API error"},
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
                                "type":        "array",
                                "items":       {"type": "string", "format": "binary"},
                                "minItems":    1,
                                "maxItems":    4,
                                "description": "1–4 scalp/hair photos (jpeg/png/webp/gif, max 10 MB each).",
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
    images:  List[UploadFile] = File(..., description="1–4 scalp/hair photos."),
    user_id: str              = Depends(_get_current_user_id),
) -> HairScalpScanResponse:

    # ── Load user profile → personalised prompt + snapshot ───────────────────
    profile          = await _get_user_profile(user_id)
    profile_snapshot = _build_profile_snapshot(profile)
    system_prompt    = _build_personalized_system_prompt(profile)

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if not is_vision_available():
        logger.info("MOCK_MODE — hair/scalp scan for user %s", user_id)
        try:
            scan_id = await _save_hair_scalp_scan(
                user_id, MOCK_RESPONSE, profile_snapshot,
                image_count=len(images), is_mock=True,
            )
            return MOCK_RESPONSE.model_copy(update={"scan_id": scan_id})
        except Exception as exc:
            logger.error("Failed to save mock hair/scalp scan: %s", exc)
            return MOCK_RESPONSE

    # ── Count check ───────────────────────────────────────────────────────────
    if not images:
        raise _err(400, "NO_IMAGES", "No images provided. Upload 1–4 scalp or hair photos.")
    if len(images) > MAX_IMAGES:
        raise _err(400, "TOO_MANY_IMAGES",
                   f"Maximum {MAX_IMAGES} images allowed; you sent {len(images)}.")

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

    # ── Claude Vision (personalised) ──────────────────────────────────────────
    result = await _call_claude_async(encoded, system_prompt)

    # ── Save to DB ────────────────────────────────────────────────────────────
    try:
        scan_id = await _save_hair_scalp_scan(
            user_id, result, profile_snapshot,
            image_count=len(images), is_mock=False,
        )
        result = result.model_copy(update={"scan_id": scan_id})
    except Exception as exc:
        logger.error("Failed to save hair/scalp scan: %s", exc)

    return result


@router.get(
    "/hair_scalp/history",
    response_model = List[ScanHistoryItem],
    summary        = "Hair & Scalp Scan History",
    description    = (
        "Returns the authenticated user's past hair/scalp scans, newest first.\n\n"
        "Each item includes `profile_snapshot` — the user's hair profile **at the time "
        "of the scan** — so comparisons stay meaningful even after profile updates.\n\n"
        "Use `limit` / `skip` for pagination."
    ),
)
async def hair_scalp_scan_history(
    user_id: str = Depends(_get_current_user_id),
    limit:   int = Query(default=20, ge=1, le=100, description="Results to return (max 100)"),
    skip:    int = Query(default=0,  ge=0,          description="Results to skip for pagination"),
) -> List[ScanHistoryItem]:
    db   = get_db()
    docs = await (
        db["scan_results"]
        .find({"user_id": user_id, "scan_type": "hair_scalp"}, sort=[("scanned_at", -1)])
        .skip(skip)
        .limit(limit)
        .to_list(length=limit)
    )

    items: list[ScanHistoryItem] = []
    for doc in docs:
        triggers = [
            DetectedTrigger(
                trigger_name  = t.get("trigger_name",  "Unknown"),
                trigger_level = t.get("trigger_level", "low"),
                cure_advice   = t.get("cure_advice",   ""),
            )
            for t in doc.get("detected_triggers", [])
        ]
        scanned_at = doc.get("scanned_at", "")
        items.append(ScanHistoryItem(
            scan_id           = str(doc["_id"]),
            score             = doc.get("score", 0),
            advice            = doc.get("advice", ""),
            detected_triggers = triggers,
            scanned_at        = scanned_at.isoformat() if isinstance(scanned_at, datetime) else str(scanned_at),
            is_mock           = doc.get("is_mock", False),
            image_count       = doc.get("image_count", 0),
            profile_snapshot  = doc.get("profile_snapshot", {}),
        ))

    return items