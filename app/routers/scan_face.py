# routers/scan_face.py
"""
POST /scan/face
GET  /scan/face/history

Accepts exactly 5 face images, validates them locally (size, format, magic
bytes, blur), personalises the Claude system prompt from the user's saved
profile, then analyses with Claude Vision.

Personalisation context injected into every Claude call:
  skin_type, skin_concerns, current_phase, allergies, budget

Scan results are saved to MongoDB `scan_results` after every successful call
(including mock) so users can compare their skin progress over time.

History endpoint returns past scans newest-first with full trigger detail.

Response shape (success):
  {
    "scan_id":           str,          # MongoDB _id — use for history linking
    "score":             int,          # 0-100
    "advice":            str,          # 2 sentences, 17-20 words
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
from app.utils.overlay_utils import generate_condition_overlay, generate_redness_overlay
from app.services.overlay_storage import upload_overlay_to_s3

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

MAX_IMAGES       = 5
MAX_FILE_SIZE_MB = 10
MAX_FILE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024
BLUR_THRESHOLD   = 90.0
RESIZE_MAX_PX    = 1024
JPEG_QUALITY     = 82

# ── Magic-byte type sniffing ──────────────────────────────────────────────────


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


class Region(BaseModel):
    """Normalised bounding box (0.0–1.0) relative to the analysed image."""
    x:      float
    y:      float
    width:  float
    height: float


class DetectedTrigger(BaseModel):
    trigger_name:  str
    trigger_level: str   # low | medium | high
    cure_advice:   str
    regions:       Optional[List[Region]] = None
    image_url:     Optional[str] = None


class VisibleRedness(BaseModel):
    """Dedicated redness analysis — separate from the trigger list."""
    score:     int                              # 0-100 redness intensity
    regions:   Optional[List[Region]] = None
    image_url: Optional[str] = None


class FaceScanResponse(BaseModel):
    scan_id:            Optional[str] = None
    score:              int
    advice:             str
    detected_triggers:  List[DetectedTrigger]
    visible_redness:    Optional[VisibleRedness] = None


class ScanHistoryItem(BaseModel):
    scan_id:           str
    score:             int
    advice:            str
    detected_triggers: List[DetectedTrigger]
    visible_redness:   Optional[VisibleRedness] = None
    scanned_at:        str   # ISO 8601
    is_mock:           bool
    image_count:       int
    profile_snapshot:  dict  # profile fields captured at time of scan


# ── Mock response ─────────────────────────────────────────────────────────────
# Shape is identical to a real Claude response so frontend code works the same
# in MOCK_MODE=true and in production.

MOCK_RESPONSE = FaceScanResponse(
    score  = 74,
    advice = (
        "Your skin shows mild oiliness and early acne along the T-zone. "
        "A salicylic acid cleanser morning and night will help greatly."
    ),
    visible_redness = VisibleRedness(
        score   = 24,
        regions = [Region(x=0.30, y=0.25, width=0.40, height=0.20)],
    ),
    detected_triggers = [
        DetectedTrigger(
            trigger_name  = "Acne / Pimples",
            trigger_level = "medium",
            cure_advice   = "Use salicylic acid cleanser twice daily.",
            regions       = [
                Region(x=0.42, y=0.35, width=0.08, height=0.07),
                Region(x=0.55, y=0.28, width=0.06, height=0.06),
            ],
        ),
        DetectedTrigger(
            trigger_name  = "Oiliness",
            trigger_level = "medium",
            cure_advice   = "Apply oil-free niacinamide moisturiser every morning.",
            regions       = [Region(x=0.30, y=0.15, width=0.40, height=0.30)],
        ),
        DetectedTrigger(
            trigger_name  = "Enlarged Pores",
            trigger_level = "low",
            cure_advice   = "Use a clay mask once or twice weekly.",
            regions       = [Region(x=0.38, y=0.40, width=0.24, height=0.15)],
        ),
        DetectedTrigger(
            trigger_name  = "Dullness",
            trigger_level = "low",
            cure_advice   = "Add a vitamin C serum to morning routine.",
            regions       = [],
        ),
    ],
)

# ── Base system prompt ────────────────────────────────────────────────────────

_BASE_SYSTEM_PROMPT = """
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
  "best_image_index": <integer 1-5, the image with the clearest frontal view>,
  "score": <integer 0-100, overall visible skin health>,
  "advice": "<exactly 2 sentences, total 17-20 words, warm and actionable>",
  "visible_redness": {
    "score": <integer 0-100, 0 = no visible redness, 100 = severe>,
    "regions": [
      { "x": <float 0-1>, "y": <float 0-1>, "width": <float 0-1>, "height": <float 0-1> }
    ]
  },
  "detected_triggers": [
    {
      "trigger_name": "<concise condition name>",
      "trigger_level": "<low|medium|high>",
      "cure_advice": "<exactly 6-7 words, specific actionable tip>",
      "regions": [
        { "x": <float 0-1>, "y": <float 0-1>, "width": <float 0-1>, "height": <float 0-1> }
      ]
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

Region coordinate rules:
  - x, y = top-left corner of the bounding box, normalised 0.0–1.0.
  - width, height = size of the bounding box, normalised 0.0–1.0.
  - Coordinates are relative to the best_image_index image.
  - Return at least 1 region per detected trigger if the area is localisable.
  - For diffuse conditions (redness, oiliness, dullness), use larger boxes
    covering the affected area.
  - If a condition is not localisable, return an empty regions array [].

Rules:
  - Return ONLY valid JSON — no markdown, no code fences, no extra keys.
  - advice: exactly 2 sentences, 17-20 words total.
  - cure_advice: exactly 6-7 words.
  - trigger_level: exactly one of: low, medium, high.
  - best_image_index: 1-based index of the clearest frontal face image.
""".strip()

# ── Profile personalisation ───────────────────────────────────────────────────

_PHASE_LABELS = {
    "on_my_period": "menstruating — hormonal breakouts and sensitivity are common right now",
    "pregnant":     "pregnant — AVOID retinoids, high-dose salicylic acid, and strong chemical exfoliants",
    "postpartum":   "postpartum — hormonal fluctuations may cause acne or hyperpigmentation",
    "menopause":    "menopausal — declining oestrogen leads to dryness and loss of elasticity",
}

_CONCERN_LABELS = {
    "acne_pimple":        "Acne / Pimples",
    "irritation_redness": "Redness / Irritation",
    "pigmentation":       "Dark Spots / Hyperpigmentation",
    "dullness":           "Dullness",
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
    Returns {} silently on any error — the scan still works, just without
    personalisation.
    """
    try:
        doc = await get_db()["users"].find_one({"_id": ObjectId(user_id)})
        if doc:
            from app.utils.pregnancy_utils import resolve_pregnancy_phase
            doc = resolve_pregnancy_phase(doc) or doc
        return doc or {}
    except Exception as exc:
        logger.warning("Could not load profile for user %s: %s", user_id, exc)
        return {}


def _build_profile_snapshot(profile: dict) -> dict:
    """
    Extract the skin/hair fields that matter for comparison over time.
    Stored alongside every scan result so history comparisons stay accurate
    even after the user later updates their profile.
    """
    return {
        "skin_type":     profile.get("skin_type"),
        "skin_concerns": profile.get("skin_concerns", []),
        "hair_type":     profile.get("hair_type"),
        "hair_concerns": profile.get("hair_concerns", []),
        "current_phase": profile.get("current_phase"),
        "life_phase":    profile.get("life_phase"),
        "allergies":     profile.get("allergies", []),
        "budget":        profile.get("budget"),
    }


def _build_personalized_system_prompt(profile: dict) -> str:
    """
    Append a USER PROFILE block to the base system prompt so Claude can:
      - Weight scoring towards the user's known skin type
      - Prioritise conditions the user already reported
      - Avoid recommending ingredients the user is allergic to
      - Tailor product price-range in cure_advice

    If the user has no profile (guest / skipped onboarding) the base prompt
    is returned unchanged — Claude still works, just without personalisation.
    """
    lines: list[str] = []

    skin_type = profile.get("skin_type")
    if skin_type:
        lines.append(f"  • Skin type: {skin_type}")

    phase = profile.get("current_phase")
    if phase:
        lines.append(f"  • Hormonal / life phase: {_PHASE_LABELS.get(phase, phase.replace('_', ' '))}")

    concerns = profile.get("skin_concerns", [])
    if concerns:
        readable = [_CONCERN_LABELS.get(c, c.replace("_", " ").title()) for c in concerns]
        lines.append(f"  • User-reported skin concerns: {', '.join(readable)}")

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
            f"match cure_advice product recommendations to this range."
        )

    if not lines:
        return _BASE_SYSTEM_PROMPT

    return (
        _BASE_SYSTEM_PROMPT
        + "\n\nUSER PROFILE — use this to personalise every part of your response:\n"
        + "\n".join(lines)
        + "\n\nInstructions: weight your score sensitivity to this skin type, "
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
                   "Retake in good lighting with the camera steady.")

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
        blocks.append({"type": "text", "text": f"Face image {i} of {len(encoded)}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
    blocks.append({
        "type": "text",
        "text": (
            "Validate all images contain the same person's clearly visible face. "
            "Then perform the full skin analysis personalised to the user profile "
            "in the system prompt. Select the image with the clearest frontal "
            "view as best_image_index. Return only the JSON schema defined above."
        ),
    })
    return blocks


# ── DB save ───────────────────────────────────────────────────────────────────


async def _save_face_scan(
    user_id:          str,
    result:           FaceScanResponse,
    profile_snapshot: dict,
    image_count:      int,
    is_mock:          bool = False,
) -> str:
    """
    Persist a face scan result to `scan_results`.

    Fields saved:
      user_id, scan_type="face", score, advice, detected_triggers,
      scanned_at (UTC), is_mock, image_count,
      profile_snapshot { skin_type, skin_concerns, hair_type, hair_concerns,
                         current_phase, allergies, budget }

    profile_snapshot is a point-in-time copy of the user's profile so that
    GET /scan/face/history comparisons stay accurate even after the user later
    updates their skin profile.
    """
    doc = {
        "user_id":   user_id,
        "scan_type": "face",
        "score":     result.score,
        "advice":    result.advice,
        "detected_triggers": [
            {
                "trigger_name":  t.trigger_name,
                "trigger_level": t.trigger_level,
                "cure_advice":   t.cure_advice,
                "regions":       [r.model_dump() for r in (t.regions or [])],
                "image_url":     t.image_url,
            }
            for t in result.detected_triggers
        ],
        "visible_redness": {
            "score":   result.visible_redness.score,
            "regions": [r.model_dump() for r in (result.visible_redness.regions or [])],
        } if result.visible_redness else None,
        "scanned_at":       datetime.now(timezone.utc),
        "is_mock":          is_mock,
        "image_count":      image_count,
        "profile_snapshot": profile_snapshot,
    }
    res = await get_db()["scan_results"].insert_one(doc)
    return str(res.inserted_id)


# ── Region parsing helper ─────────────────────────────────────────────────────


def _parse_regions(raw_regions: list) -> list[Region] | None:
    """Parse and validate normalised region coordinates from Claude's JSON."""
    if not raw_regions:
        return None
    regions: list[Region] = []
    for r in raw_regions:
        if not isinstance(r, dict):
            continue
        try:
            region = Region(
                x=float(r.get("x", 0)),
                y=float(r.get("y", 0)),
                width=float(r.get("width", 0)),
                height=float(r.get("height", 0)),
            )
            if region.width < 0.01 or region.height < 0.01:
                continue
            regions.append(region)
        except (TypeError, ValueError):
            continue
    return regions if regions else None


# ── Claude Vision call ────────────────────────────────────────────────────────


async def _call_claude_async(
    encoded:       list[tuple[str, str]],
    system_prompt: str,
) -> tuple[FaceScanResponse, int]:
    try:
        raw = await async_vision_call(system_prompt, _build_content_blocks(encoded), max_tokens=2048)
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

    if not data.get("face_detected", True):
        code_map = {
            "no_face":                   "NO_FACE",
            "non_human":                 "NON_HUMAN",
            "drawing_or_photo_of_photo": "NOT_REAL_FACE",
            "too_many_faces_unclear":    "TOO_MANY_FACES",
            "different_people":          "DIFFERENT_PEOPLE",
        }
        raise _err(
            400,
            code_map.get(data.get("error_reason", ""), "NO_FACE"),
            data.get("error_detail", "No human face detected."),
        )

    missing = [k for k in ("score", "advice", "detected_triggers") if k not in data]
    if missing:
        raise _err(422, "MISSING_FIELDS", f"Claude response missing fields: {missing}")

    try:
        score = max(0, min(100, int(data["score"])))
    except (TypeError, ValueError):
        raise _err(422, "INVALID_SCORE", f"Invalid score value: {data.get('score')}")

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
            cure_advice   = str(t.get("cure_advice",   "Consult a dermatologist.")).strip(),
            regions       = _parse_regions(t.get("regions", [])),
        ))

    # ── Visible redness ───────────────────────────────────────────────────
    vr_data = data.get("visible_redness")
    visible_redness = None
    if isinstance(vr_data, dict):
        try:
            vr_score = max(0, min(100, int(vr_data.get("score", 0))))
            vr_regions = _parse_regions(vr_data.get("regions", []))
            visible_redness = VisibleRedness(
                score=vr_score, regions=vr_regions or None,
            )
        except (TypeError, ValueError):
            logger.warning("Malformed visible_redness data: %s", vr_data)

    # ── Best image index (1-based, default 1) ─────────────────────────────
    try:
        best_idx = max(1, min(5, int(data.get("best_image_index", 1))))
    except (TypeError, ValueError):
        best_idx = 1

    return FaceScanResponse(
        score             = score,
        advice            = str(data["advice"]).strip(),
        detected_triggers = triggers,
        visible_redness   = visible_redness,
    ), best_idx


# ── Overlay generation & upload ───────────────────────────────────────────────


async def _generate_and_upload_overlays(
    encoded_images: list[tuple[str, str]],
    result:         FaceScanResponse,
    user_id:        str,
    scan_id:        str,
    best_image_idx: int,
) -> FaceScanResponse:
    """
    Post-processing: generate condition overlays and upload to S3.

    For each detected trigger with regions, draw an annotated image on the
    best face photo and upload it.  Same for visible_redness.

    Errors are logged but never fail the scan — the response is returned
    without image_url fields (graceful degradation).
    """
    # Decode the best image from base64
    idx = max(0, min(len(encoded_images) - 1, best_image_idx - 1))
    b64_data, _ = encoded_images[idx]
    image_bytes = base64.b64decode(b64_data)

    # ── Condition overlays ────────────────────────────────────────────────
    updated_triggers: list[DetectedTrigger] = []
    for trigger in result.detected_triggers:
        if trigger.regions:
            try:
                region_dicts = [r.model_dump() for r in trigger.regions]
                overlay_bytes = await asyncio.to_thread(
                    generate_condition_overlay,
                    image_bytes,
                    trigger.trigger_name,
                    region_dicts,
                )
                url = await upload_overlay_to_s3(
                    overlay_bytes, user_id, scan_id, trigger.trigger_name,
                )
                trigger = trigger.model_copy(update={"image_url": url})
            except Exception as exc:
                logger.error("Overlay failed for '%s': %s", trigger.trigger_name, exc)
        updated_triggers.append(trigger)

    # ── Redness overlay ───────────────────────────────────────────────────
    updated_redness = result.visible_redness
    if result.visible_redness and result.visible_redness.regions:
        try:
            redness_dicts = [r.model_dump() for r in result.visible_redness.regions]
            redness_bytes = await asyncio.to_thread(
                generate_redness_overlay,
                image_bytes,
                redness_dicts,
                result.visible_redness.score,
            )
            url = await upload_overlay_to_s3(
                redness_bytes, user_id, scan_id, "visible_redness",
            )
            updated_redness = result.visible_redness.model_copy(
                update={"image_url": url},
            )
        except Exception as exc:
            logger.error("Redness overlay failed: %s", exc)

    return result.model_copy(update={
        "detected_triggers": updated_triggers,
        "visible_redness":   updated_redness,
    })


async def _update_scan_overlays(scan_id: str, result: FaceScanResponse) -> None:
    """Patch the scan document with overlay image URLs after generation."""
    update: dict = {}

    update["detected_triggers"] = [
        {
            "trigger_name":  t.trigger_name,
            "trigger_level": t.trigger_level,
            "cure_advice":   t.cure_advice,
            "regions":       [r.model_dump() for r in (t.regions or [])],
            "image_url":     t.image_url,
        }
        for t in result.detected_triggers
    ]

    if result.visible_redness:
        update["visible_redness"] = {
            "score":     result.visible_redness.score,
            "regions":   [r.model_dump() for r in (result.visible_redness.regions or [])],
            "image_url": result.visible_redness.image_url,
        }

    await get_db()["scan_results"].update_one(
        {"_id": ObjectId(scan_id)},
        {"$set": update},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/face",
    response_model = FaceScanResponse,
    summary        = "Face Skin Scan",
    description    = (
        "Upload exactly 5 face photos. Each image is validated locally (format, size, blur), "
        "then sent to Claude Vision with a **personalised system prompt** built from the "
        "user's skin profile (skin type, concerns, hormonal phase, allergens, budget).\n\n"
        "Results are saved to the database. Use `GET /scan/face/history` to compare over time.\n\n"
        "**Error codes** in `detail.code`:\n"
        "- `INVALID_IMAGE_COUNT` — not exactly 5 images sent\n"
        "- `NO_FACE` / `NON_HUMAN` / `NOT_REAL_FACE` / `TOO_MANY_FACES` — image content issue\n"
        "- `DIFFERENT_PEOPLE` — images show different people\n"
        "- `BLURRY_IMAGE` — image too blurry\n"
        "- `UNSUPPORTED_FORMAT` — not jpeg/png/webp/gif\n"
        "- `FILE_TOO_LARGE` — exceeds 10 MB\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip the API call during development."
    ),
    responses={
        400: {"description": "Image or count validation failed"},
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
                                "minItems":    5,
                                "maxItems":    5,
                                "description": "Exactly 5 face photos (jpeg/png/webp/gif, max 10 MB each).",
                            }
                        },
                    }
                }
            },
            "required": True,
        }
    },
)
async def face_scan(
    images:  List[UploadFile] = File(..., description="Exactly 5 face photos."),
    user_id: str              = Depends(_get_current_user_id),
) -> FaceScanResponse:

    # ── Load user profile → personalised prompt + snapshot ───────────────────
    profile          = await _get_user_profile(user_id)
    profile_snapshot = _build_profile_snapshot(profile)
    system_prompt    = _build_personalized_system_prompt(profile)

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if not is_vision_available():
        logger.info("MOCK_MODE — face scan for user %s", user_id)
        try:
            scan_id = await _save_face_scan(
                user_id, MOCK_RESPONSE, profile_snapshot,
                image_count=len(images), is_mock=True,
            )
            return MOCK_RESPONSE.model_copy(update={"scan_id": scan_id})
        except Exception as exc:
            logger.error("Failed to save mock face scan: %s", exc)
            return MOCK_RESPONSE

    # ── Image count check ─────────────────────────────────────────────────────
    if len(images) != MAX_IMAGES:
        raise _err(
            400, "INVALID_IMAGE_COUNT",
            f"Exactly {MAX_IMAGES} face photos are required; you sent {len(images)}.",
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

    # ── Claude Vision (personalised) ──────────────────────────────────────────
    result, best_image_index = await _call_claude_async(encoded, system_prompt)

    # ── Save to DB ────────────────────────────────────────────────────────────
    scan_id = None
    try:
        scan_id = await _save_face_scan(
            user_id, result, profile_snapshot,
            image_count=len(images), is_mock=False,
        )
        result = result.model_copy(update={"scan_id": scan_id})
    except Exception as exc:
        logger.error("Failed to save face scan: %s", exc)

    # ── Generate & upload overlay images ──────────────────────────────────────
    if scan_id:
        try:
            result = await _generate_and_upload_overlays(
                encoded, result, user_id, scan_id, best_image_index,
            )
            await _update_scan_overlays(scan_id, result)
        except Exception as exc:
            logger.error("Overlay generation failed (non-fatal): %s", exc)

    return result


@router.get(
    "/face/history",
    response_model = List[ScanHistoryItem],
    summary        = "Face Scan History",
    description    = (
        "Returns the authenticated user's past face scans, newest first.\n\n"
        "Each item includes `profile_snapshot` — the user's skin profile **at the time "
        "of the scan** — so comparisons stay meaningful even after the user updates their profile.\n\n"
        "Use `limit` / `skip` for pagination."
    ),
)
async def face_scan_history(
    user_id: str = Depends(_get_current_user_id),
    limit:   int = Query(default=20, ge=1, le=100, description="Results to return (max 100)"),
    skip:    int = Query(default=0,  ge=0,          description="Results to skip for pagination"),
) -> List[ScanHistoryItem]:
    db   = get_db()
    docs = await (
        db["scan_results"]
        .find({"user_id": user_id, "scan_type": "face"}, sort=[("scanned_at", -1)])
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
                regions       = _parse_regions(t.get("regions", [])),
                image_url     = t.get("image_url"),
            )
            for t in doc.get("detected_triggers", [])
        ]
        # ── Visible redness from DB ───────────────────────────────────────
        vr_doc = doc.get("visible_redness")
        visible_redness = None
        if isinstance(vr_doc, dict):
            visible_redness = VisibleRedness(
                score     = vr_doc.get("score", 0),
                regions   = _parse_regions(vr_doc.get("regions", [])),
                image_url = vr_doc.get("image_url"),
            )
        scanned_at = doc.get("scanned_at", "")
        items.append(ScanHistoryItem(
            scan_id           = str(doc["_id"]),
            score             = doc.get("score", 0),
            advice            = doc.get("advice", ""),
            detected_triggers = triggers,
            visible_redness   = visible_redness,
            scanned_at        = scanned_at.isoformat() if isinstance(scanned_at, datetime) else str(scanned_at),
            is_mock           = doc.get("is_mock", False),
            image_count       = doc.get("image_count", 0),
            profile_snapshot  = doc.get("profile_snapshot", {}),
        ))

    return items