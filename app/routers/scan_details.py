# routers/scan_details.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Scan Detail Breakdown                       ║
║                                                                  ║
║  Endpoints:                                                      ║
║   GET /scan/face/details/{scan_id}                              ║
║   GET /scan/hair_scalp/details/{scan_id}                        ║
║                                                                  ║
║  Per-trigger detail sections:                                    ║
║   • why_this_happening  — root cause (2-3 sentences)            ║
║   • what_to_stop        — habits/ingredients to avoid (3-5)     ║
║   • what_to_do          — actionable steps (3-5)                ║
║   • lifestyle_factors   — diet, sleep, stress, env (3-4)        ║
║   • learn_more          — educational paragraph (2-3 sentences) ║
║                                                                  ║
║  Cache strategy:                                                 ║
║   Details are generated ONCE by Claude and saved inside the     ║
║   scan_results document under the `details` field.              ║
║   All subsequent calls return from cache — no LLM call made.    ║
║   Check `from_cache: true` in the response to confirm.          ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt as jose_jwt
from pydantic import BaseModel

from app.clients.claude_client import ClaudeClient
from app.core.database import get_db

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


# ── Schemas ───────────────────────────────────────────────────────────────────

class TriggerDetail(BaseModel):
    trigger_name:       str
    why_this_happening: str
    what_to_stop:       List[str]
    what_to_do:         List[str]
    lifestyle_factors:  List[str]
    learn_more:         str


class DetectedTriggerBase(BaseModel):
    trigger_name:  str
    trigger_level: str
    cure_advice:   str


class ScanDetailsResponse(BaseModel):
    scan_id:              str
    scan_type:            str        # face | hair_scalp
    score:                int
    advice:               str
    detected_triggers:    List[DetectedTriggerBase]
    details:              List[TriggerDetail]
    details_generated_at: Optional[str] = None
    from_cache:           bool = False


# ── Claude system prompt ──────────────────────────────────────────────────────

_DETAILS_SYSTEM = """\
You are a professional dermatologist and trichologist AI embedded in a skincare app.
You will receive a list of detected skin or scalp conditions from a user's scan.
For EACH condition, generate a detailed educational breakdown.

Return ONLY a valid JSON array — no markdown, no code fences, no extra text.
Start with [ and end with ].

Schema for each element:
{
  "trigger_name": "<exact condition name as provided>",
  "why_this_happening": "<2-3 sentences: root cause — biological, hormonal, or environmental>",
  "what_to_stop": ["<item>", "<item>", "<item>"],
  "what_to_do": ["<step>", "<step>", "<step>", "<step>"],
  "lifestyle_factors": ["<factor>", "<factor>", "<factor>"],
  "learn_more": "<2-3 sentences of deeper educational context mentioning relevant treatments>"
}

Field rules:
  what_to_stop       : 3-5 specific habits, ingredients, or products to avoid.
                       Each item: 5-10 words.
  what_to_do         : 3-5 concrete, actionable steps. Each: 5-12 words.
  lifestyle_factors  : 3-4 items covering diet, sleep, stress, hydration, environment.
                       Each: 5-10 words.
  why_this_happening : 2-3 complete sentences; explain the biology / root cause clearly.
  learn_more         : 2-3 sentences of deeper context; name relevant active ingredients
                       or clinical treatments.

Tone: professional yet warm, like a caring specialist.
Severity note: only recommend seeing a doctor for high-severity conditions.
Personalise: adjust advice intensity to the trigger_level (low/medium/high).
Allergen note: if allergies are listed in the user profile, never mention those
  ingredients in what_to_do or learn_more.
Return ONLY the JSON array. Nothing else.
""".strip()


def _build_details_prompt(
    scan_type:        str,
    triggers:         list[dict],
    profile_snapshot: dict,
) -> str:
    specialist = "dermatologist" if scan_type == "face" else "trichologist"

    trigger_lines = "\n".join(
        f"  - {t['trigger_name']} (level: {t.get('trigger_level','low')}): "
        f"{t.get('cure_advice','')}"
        for t in triggers
    )

    profile_parts: list[str] = []
    if profile_snapshot.get("skin_type"):
        profile_parts.append(f"skin type: {profile_snapshot['skin_type']}")
    if profile_snapshot.get("hair_type"):
        profile_parts.append(f"hair type: {profile_snapshot['hair_type'].replace('_',' ')}")
    if profile_snapshot.get("current_phase"):
        profile_parts.append(
            f"hormonal phase: {profile_snapshot['current_phase'].replace('_',' ')}"
        )
    allergies = profile_snapshot.get("allergies") or []
    if allergies:
        profile_parts.append(f"known allergens (never recommend these): {', '.join(allergies)}")
    if profile_snapshot.get("budget"):
        profile_parts.append(f"budget preference: {profile_snapshot['budget'].replace('_',' ')}")

    profile_str = "\n  ".join(profile_parts) if profile_parts else "no profile data"

    return (
        f"Acting as a {specialist}, generate detailed breakdowns for all conditions below.\n\n"
        f"User profile:\n  {profile_str}\n\n"
        f"Detected conditions ({scan_type.replace('_','/')} scan):\n{trigger_lines}\n\n"
        "Return a JSON array with one object per condition. Include ALL conditions listed above."
    )


# ── Mock fallback ──────────────────────────────────────────────────────────────

def _mock_details(triggers: list[dict]) -> list[dict]:
    """Deterministic mock details — used in MOCK_MODE and on any Claude error."""
    result = []
    for t in triggers:
        name  = t.get("trigger_name", "Unknown Condition")
        level = t.get("trigger_level", "low")
        result.append({
            "trigger_name": name,
            "why_this_happening": (
                f"{name} typically occurs when the skin's sebaceous balance or barrier "
                f"function is disrupted. Factors like excess sebum, environmental stressors, "
                f"hormonal shifts, or incorrect product use can all contribute. "
                f"{'Without targeted treatment, it may worsen.' if level != 'low' else 'With care, this is very manageable.'}"
            ),
            "what_to_stop": [
                "Avoid harsh cleansers that strip the skin barrier",
                "Stop using products with alcohol or synthetic fragrance",
                "Avoid touching or picking the affected area",
                "Reduce use of occlusive, pore-clogging products",
            ],
            "what_to_do": [
                "Cleanse gently with a pH-balanced, sulfate-free cleanser",
                "Apply a targeted active serum addressing this condition",
                "Use a non-comedogenic, fragrance-free moisturiser daily",
                "Apply SPF 30+ broad-spectrum sunscreen every morning",
            ],
            "lifestyle_factors": [
                "Manage stress through consistent sleep and exercise",
                "Drink at least 8 glasses of water daily for skin hydration",
                "Reduce intake of high-glycaemic foods and dairy if prone to breakouts",
            ],
            "learn_more": (
                f"{name} is one of the most frequently addressed conditions in dermatology. "
                "Evidence supports the use of ingredients like niacinamide, ceramides, "
                "and hyaluronic acid for many skin barrier concerns. "
                "Consistent use of a minimal, targeted routine typically yields visible "
                "improvement within 4-8 weeks."
            ),
        })
    return result


# ── Core generator ────────────────────────────────────────────────────────────

async def _generate_details(scan_doc: dict, scan_type: str) -> list[dict]:
    """
    Call ClaudeClient.text() to generate detailed trigger breakdowns.
    Falls back to _mock_details() on MOCK_MODE, missing triggers, or any error.
    """
    triggers = scan_doc.get("detected_triggers", [])
    if not triggers:
        return []

    if False:
        logger.info("MOCK_MODE active — returning mock scan details")
        return _mock_details(triggers)

    profile_snapshot = scan_doc.get("profile_snapshot", {})
    prompt           = _build_details_prompt(scan_type, triggers, profile_snapshot)
    client           = ClaudeClient()

    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = _DETAILS_SYSTEM,
                user       = prompt,
                max_tokens = 2048,
            ),
        )
    except Exception as exc:
        logger.error("Details LLM call failed: %s", exc)
        return _mock_details(triggers)

    if not raw:
        logger.error("Details LLM returned empty response")
        return _mock_details(triggers)

    # Strip markdown fences if model added them
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    # Ensure it starts with [ (array)
    idx = raw.find("[")
    if idx > 0:
        raw = raw[idx:]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Details JSON parse error: %s | raw: %s", exc, raw[:300])
        return _mock_details(triggers)

    if not isinstance(data, list):
        logger.error("Details response is not a JSON array: %s", raw[:200])
        return _mock_details(triggers)

    # Sanitise each item
    cleaned: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "trigger_name":       str(item.get("trigger_name", "Unknown")).strip(),
            "why_this_happening": str(item.get("why_this_happening", "")).strip(),
            "what_to_stop":       [str(s).strip() for s in item.get("what_to_stop", []) if str(s).strip()],
            "what_to_do":         [str(s).strip() for s in item.get("what_to_do",   []) if str(s).strip()],
            "lifestyle_factors":  [str(s).strip() for s in item.get("lifestyle_factors", []) if str(s).strip()],
            "learn_more":         str(item.get("learn_more", "")).strip(),
        })

    return cleaned if cleaned else _mock_details(triggers)


# ── Shared handler (face + hair_scalp) ────────────────────────────────────────

async def _get_scan_details(
    scan_id:   str,
    scan_type: str,   # "face" | "hair_scalp"
    user_id:   str,
) -> ScanDetailsResponse:
    """
    1. Validate scan ownership.
    2. Return cached details if they exist.
    3. Otherwise generate via Claude, cache in DB, then return.
    """
    # ── Validate scan_id ──────────────────────────────────────────────────────
    try:
        oid = ObjectId(scan_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scan ID format.")

    db  = get_db()
    doc = await db["scan_results"].find_one(
        {"_id": oid, "user_id": user_id, "scan_type": scan_type}
    )
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Scan not found or does not belong to this user.",
        )

    # ── Build DetectedTriggerBase list (always returned) ─────────────────────
    detected_triggers = [
        DetectedTriggerBase(
            trigger_name  = t.get("trigger_name",  "Unknown"),
            trigger_level = t.get("trigger_level", "low"),
            cure_advice   = t.get("cure_advice",   ""),
        )
        for t in doc.get("detected_triggers", [])
    ]

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cached = doc.get("details")
    if cached and isinstance(cached, list) and cached:
        logger.info("Cache hit: returning details for scan %s", scan_id)
        details = [
            TriggerDetail(
                trigger_name       = d.get("trigger_name",       "Unknown"),
                why_this_happening = d.get("why_this_happening", ""),
                what_to_stop       = d.get("what_to_stop",       []),
                what_to_do         = d.get("what_to_do",         []),
                lifestyle_factors  = d.get("lifestyle_factors",  []),
                learn_more         = d.get("learn_more",         ""),
            )
            for d in cached
        ]
        raw_ts = doc.get("details_generated_at")
        ts_str = (
            raw_ts.isoformat() if isinstance(raw_ts, datetime)
            else str(raw_ts) if raw_ts
            else None
        )
        return ScanDetailsResponse(
            scan_id              = scan_id,
            scan_type            = scan_type,
            score                = doc.get("score", 0),
            advice               = doc.get("advice", ""),
            detected_triggers    = detected_triggers,
            details              = details,
            details_generated_at = ts_str,
            from_cache           = True,
        )

    # ── Cache miss → generate ─────────────────────────────────────────────────
    logger.info("Generating details for scan %s (type=%s, triggers=%d)",
                scan_id, scan_type, len(doc.get("detected_triggers", [])))

    raw_details = await _generate_details(doc, scan_type)

    # ── Persist to DB ─────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    try:
        await db["scan_results"].update_one(
            {"_id": oid},
            {"$set": {"details": raw_details, "details_generated_at": now}},
        )
    except Exception as exc:
        logger.error("Failed to cache details for scan %s: %s", scan_id, exc)

    details = [
        TriggerDetail(
            trigger_name       = d.get("trigger_name",       "Unknown"),
            why_this_happening = d.get("why_this_happening", ""),
            what_to_stop       = d.get("what_to_stop",       []),
            what_to_do         = d.get("what_to_do",         []),
            lifestyle_factors  = d.get("lifestyle_factors",  []),
            learn_more         = d.get("learn_more",         ""),
        )
        for d in raw_details
    ]

    return ScanDetailsResponse(
        scan_id              = scan_id,
        scan_type            = scan_type,
        score                = doc.get("score", 0),
        advice               = doc.get("advice", ""),
        detected_triggers    = detected_triggers,
        details              = details,
        details_generated_at = now.isoformat(),
        from_cache           = False,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/face/details/{scan_id}",
    response_model = ScanDetailsResponse,
    summary        = "Face Scan — Detailed Trigger Breakdown",
    description    = (
        "Returns a rich educational breakdown for every trigger detected in a face scan.\n\n"
        "**Sections per trigger:**\n"
        "- `why_this_happening` — biological/hormonal/environmental root cause (2-3 sentences)\n"
        "- `what_to_stop` — specific habits, ingredients, and products to avoid\n"
        "- `what_to_do` — concrete, actionable improvement steps\n"
        "- `lifestyle_factors` — diet, sleep, stress, hydration, and environment tips\n"
        "- `learn_more` — deeper educational context with ingredient/treatment names\n\n"
        "**Caching:** Details are generated **once** by Claude and stored in the database. "
        "All subsequent requests for the same `scan_id` return instantly from cache "
        "(`from_cache: true`). No extra API cost after the first call.\n\n"
        "Use the `scan_id` returned by `POST /scan/face`."
    ),
    responses={
        400: {"description": "Invalid scan ID format"},
        401: {"description": "Missing or invalid JWT"},
        404: {"description": "Scan not found or does not belong to this user"},
    },
)
async def face_scan_details(
    scan_id: str,
    user_id: str = Depends(_get_current_user_id),
) -> ScanDetailsResponse:
    return await _get_scan_details(scan_id, "face", user_id)


@router.get(
    "/hair_scalp/details/{scan_id}",
    response_model = ScanDetailsResponse,
    summary        = "Hair & Scalp Scan — Detailed Trigger Breakdown",
    description    = (
        "Returns a rich educational breakdown for every trigger detected in a hair/scalp scan.\n\n"
        "**Sections per trigger:**\n"
        "- `why_this_happening` — root cause from a trichology perspective (2-3 sentences)\n"
        "- `what_to_stop` — habits, ingredients, and products to avoid\n"
        "- `what_to_do` — concrete improvement steps\n"
        "- `lifestyle_factors` — diet, stress, water quality, brushing, styling habits\n"
        "- `learn_more` — deeper educational context with ingredient/treatment names\n\n"
        "**Caching:** Details are generated **once** by Claude and stored in the database. "
        "All subsequent requests for the same `scan_id` return instantly from cache "
        "(`from_cache: true`).\n\n"
        "Use the `scan_id` returned by `POST /scan/hair_scalp`."
    ),
    responses={
        400: {"description": "Invalid scan ID format"},
        401: {"description": "Missing or invalid JWT"},
        404: {"description": "Scan not found or does not belong to this user"},
    },
)
async def hair_scalp_scan_details(
    scan_id: str,
    user_id: str = Depends(_get_current_user_id),
) -> ScanDetailsResponse:
    return await _get_scan_details(scan_id, "hair_scalp", user_id)