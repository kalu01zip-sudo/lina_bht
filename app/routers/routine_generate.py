# routers/routine_generate.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — AI Routine Generator                        ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /scan/face/{scan_id}/generate-routine                    ║
║   POST /scan/hair_scalp/{scan_id}/generate-routine              ║
║   POST /scan/product/{scan_id}/generate-routine                 ║
║                                                                  ║
║  Description shape (100-150 words, compact JSON):               ║
║   what      → 2 sentences: product purpose + why for this user  ║
║   how_to_use → 3-4 numbered steps (plain text)                  ║
║   tip        → 1 expert tip                                     ║
║   warning    → 1 key caution                                    ║
║                                                                  ║
║  TWO-PHASE GENERATION:                                           ║
║   Phase 1 — metadata (needs_update + step fields, no desc)      ║
║   Phase 2 — compact JSON description per step (concurrent)      ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Annotated, List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.clients.claude_client import ClaudeClient, USE_LOCAL_LLM
from app.core.database import get_db
from app.routers.auth import _get_current_user
from app.routers.admin_product import find_products_for_conditions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scan", tags=["Scan"])

CurrentUser = Annotated[dict, Depends(_get_current_user)]
TimeSlot    = Literal["morning", "night", "weekly"]


# ── Category icon map (Source B — face/hair scan generated steps) ─────────────
#
# These steps are AI-invented (e.g. "Salicylic Acid Cleanser") — no real product
# exists, so no barcode image is available. We resolve a generic category icon
# by matching keywords in the product_name. Host these icons on your own CDN/S3.
#
# Replace the URLs below with your real asset URLs before going to production.

_CATEGORY_ICONS: dict[str, str] = {
    "cleanser":    "https://cdn.skinsense.app/icons/cleanser.png",
    "wash":        "https://cdn.skinsense.app/icons/cleanser.png",
    "foam":        "https://cdn.skinsense.app/icons/cleanser.png",
    "serum":       "https://cdn.skinsense.app/icons/serum.png",
    "retinol":     "https://cdn.skinsense.app/icons/serum.png",
    "niacinamide": "https://cdn.skinsense.app/icons/serum.png",
    "vitamin c":   "https://cdn.skinsense.app/icons/serum.png",
    "moisturiser": "https://cdn.skinsense.app/icons/moisturiser.png",
    "moisturizer": "https://cdn.skinsense.app/icons/moisturiser.png",
    "cream":       "https://cdn.skinsense.app/icons/moisturiser.png",
    "lotion":      "https://cdn.skinsense.app/icons/moisturiser.png",
    "sunscreen":   "https://cdn.skinsense.app/icons/spf.png",
    "spf":         "https://cdn.skinsense.app/icons/spf.png",
    "toner":       "https://cdn.skinsense.app/icons/toner.png",
    "mist":        "https://cdn.skinsense.app/icons/toner.png",
    "exfoliant":   "https://cdn.skinsense.app/icons/exfoliant.png",
    "bha":         "https://cdn.skinsense.app/icons/exfoliant.png",
    "aha":         "https://cdn.skinsense.app/icons/exfoliant.png",
    "mask":        "https://cdn.skinsense.app/icons/mask.png",
    "clay":        "https://cdn.skinsense.app/icons/mask.png",
    "shampoo":     "https://cdn.skinsense.app/icons/shampoo.png",
    "conditioner": "https://cdn.skinsense.app/icons/conditioner.png",
    "oil":         "https://cdn.skinsense.app/icons/hair_oil.png",
    "hair oil":    "https://cdn.skinsense.app/icons/hair_oil.png",
    "scalp":       "https://cdn.skinsense.app/icons/shampoo.png",
}
_DEFAULT_ICON = "https://cdn.skinsense.app/icons/product_default.png"


def _category_icon(product_name: str) -> str:
    """
    Returns a category icon URL for AI-generated steps (Source B).
    Checks product_name keywords against _CATEGORY_ICONS dict.
    Falls back to _DEFAULT_ICON when no keyword matches.
    """
    name_lower = product_name.lower()
    for keyword, url in _CATEGORY_ICONS.items():
        if keyword in name_lower:
            return url
    return _DEFAULT_ICON


# ── Schemas ───────────────────────────────────────────────────────────────────

class StepDescription(BaseModel):
    """Compact, structured description stored per step."""
    what:        str               # 2 sentences: what + why for this user
    how_to_use:  list[str]         # 3-4 plain-text numbered steps
    tip:         str               # 1 pro tip
    warning:     str               # 1 key caution


class GeneratedStep(BaseModel):
    step_id:           str
    time_slot:         TimeSlot
    title:             str
    product_name:      str
    bio:               str
    description:       StepDescription
    reason:            str
    product_image_url: Optional[str] = None   # OBF image (Source A) or category icon (Source B)


class RoutineStepSummary(BaseModel):
    step_id:           str
    time_slot:         TimeSlot
    title:             Optional[str]
    product_name:      str
    bio:               Optional[str]
    description:       Optional[StepDescription]
    product_image_url: Optional[str] = None   # OBF image (Source A) or category icon (Source B)


class RoutineGenerateResponse(BaseModel):
    routine_updated: bool
    message:         str
    added_steps:     List[GeneratedStep]
    existing_steps:  List[RoutineStepSummary]
    scan_id:         str
    scan_type:       str


# ── Phase 1 system prompt ─────────────────────────────────────────────────────
# Deliberately excludes description — keeps JSON tiny for reliable parsing.

_META_SYSTEM = """\
You are a skincare/haircare routine advisor in the GIXY app.
Analyse the scan results and existing routine, then decide if a new step is needed.

Return ONLY valid JSON — no markdown, no extra text.

No update needed:
{"needs_update":false,"message":"<1 friendly sentence confirming routine is on track>"}

Update needed:
{"needs_update":true,"message":"<1 warm sentence: what you're adding and why>","new_step":[{"time_slot":"<morning|night|weekly>","title":"<2-4 words linked to product>","product_name":"<generic category, 2-6 words, no brands>","bio":"<10-15 words, present tense, action-focused>","reason":"<8-12 words: why this addresses the scan finding>"}]}

RULES:
- One step only. Pick the highest-severity condition.
- No duplicates of products already in the existing routine.
- No allergens from the user profile.
- Budget: budget_friendly→drugstore, midrange→mid-range, premium→luxury.
- Pregnancy: avoid retinoids, high-dose salicylic acid, benzoyl peroxide, essential oils.
- Timing: vitamin C→morning, retinol→night, clay mask→weekly, SPF→morning only.
- Return ONLY the JSON.\
""".strip()


# ── Phase 2 system prompt ─────────────────────────────────────────────────────
# Returns compact JSON — 4 fields, ~100-150 words total.

_DESC_SYSTEM = """\
You are a professional skincare advisor writing a concise usage guide for one routine step.

Return ONLY valid JSON — no markdown, no extra text, no preamble:
{
  "what":       "<2 sentences: what this product type does + why it suits this user's specific condition. 25-35 words total.>",
  "how_to_use": ["<step 1>", "<step 2>", "<step 3>"],
  "tip":        "<1 expert tip specific to this product and user condition. 15-20 words.>",
  "warning":    "<1 key caution or when-to-stop signal. 10-15 words.>"
}

RULES:
- how_to_use: exactly 3 steps, plain text, action verbs, no HTML.
- tip and warning: one sentence each, no bullet characters.
- Total word count across all fields: 100-150 words.
- No brand names. Generic ingredient/category language only.
- Return ONLY the JSON.\
""".strip()


# ── Prompt builders ────────────────────────────────────────────────────────────

def _fmt_profile(user: dict) -> str:
    parts = []
    if user.get("skin_type"):    parts.append(f"skin:{user['skin_type']}")
    if user.get("hair_type"):    parts.append(f"hair:{user['hair_type']}")
    if user.get("current_phase"):parts.append(f"phase:{user['current_phase']}")
    sc = user.get("skin_concerns") or []
    if sc: parts.append(f"skin_concerns:{','.join(sc)}")
    hc = user.get("hair_concerns") or []
    if hc: parts.append(f"hair_concerns:{','.join(hc)}")
    al = user.get("allergies") or []
    if al: parts.append(f"ALLERGENS(avoid):{','.join(al)}")
    if user.get("budget"): parts.append(f"budget:{user['budget']}")
    return " | ".join(parts) if parts else "no profile"


def _fmt_routine(steps: list[dict]) -> str:
    if not steps:
        return "(empty — no existing steps)"
    by_slot: dict[str, list[str]] = {"morning": [], "night": [], "weekly": []}
    for s in steps:
        slot = s.get("time_slot", "morning")
        if slot in by_slot:
            by_slot[slot].append(s.get("product_name", "?"))
    parts = []
    for slot, items in by_slot.items():
        if items:
            parts.append(f"{slot}: {', '.join(items)}")
    return " | ".join(parts) if parts else "(empty)"


def _fmt_conditions(scan_doc: dict) -> str:
    lines = []
    for t in scan_doc.get("detected_triggers", []):
        if isinstance(t, dict):
            lines.append(
                f"{t.get('trigger_name','?')} "
                f"({t.get('trigger_level','?')}) — {t.get('cure_advice','')}"
            )
    for c in scan_doc.get("detected_conditions", []):
        if isinstance(c, dict):
            lines.append(
                f"{c.get('condition','?')} "
                f"({c.get('seriousness','?')}) — {c.get('detail','')}"
            )
    return "; ".join(lines) if lines else "none detected"


def _extract_condition_slugs(scan_doc: dict) -> list[str]:
    """
    Extract a flat list of condition/trigger slug strings from a scan document.
    Used to query the product catalogue for real product matches.
    """
    slugs: list[str] = []
    for t in scan_doc.get("detected_triggers", []):
        if isinstance(t, dict):
            name = t.get("trigger_name", "")
            if name:
                slugs.append(name.strip().lower().replace(" ", "_"))
    for c in scan_doc.get("detected_conditions", []):
        if isinstance(c, dict):
            cond = c.get("condition", "")
            if cond:
                slugs.append(cond.strip().lower().replace(" ", "_"))
    return slugs


def _pick_db_product(scan_doc: dict) -> dict | None:
    """
    Try to find a real product from the admin catalogue that targets
    the conditions detected in this scan.
    Returns the first matched product dict, or None if catalogue is empty.
    """
    slugs = _extract_condition_slugs(scan_doc)
    if not slugs:
        return None
    matches = find_products_for_conditions(slugs, limit=1)
    return matches[0] if matches else None


def _build_meta_prompt(
    scan_doc: dict,
    scan_type: str,
    user: dict,
    existing: list[dict],
) -> str:
    score = scan_doc.get("score", 0)
    note  = (
        scan_doc.get("advice")
        or scan_doc.get("score_recommendation_note")
        or ""
    )
    label = "face" if scan_type == "face" else "hair/scalp"
    return (
        f"SCAN ({label}, score {score}/100): {note}\n"
        f"CONDITIONS: {_fmt_conditions(scan_doc)}\n"
        f"PROFILE: {_fmt_profile(user)}\n"
        f"EXISTING ROUTINE: {_fmt_routine(existing)}\n"
        "Decide if a new routine step is needed. Return JSON."
    )


def _build_product_meta_prompt(
    scan_doc: dict,
    user: dict,
    existing: list[dict],
) -> str:
    name    = scan_doc.get("product_name", "product")
    brand   = scan_doc.get("brand", "")
    best_use= scan_doc.get("best_use", "")
    label   = f"{brand} {name}".strip()
    return (
        f"PRODUCT: {label}\n"
        f"BEST USE: {best_use}\n"
        f"PROFILE: {_fmt_profile(user)}\n"
        f"EXISTING ROUTINE: {_fmt_routine(existing)}\n"
        "Generate a step showing how to add this product to the routine. Return JSON."
    )


def _build_desc_prompt(step: dict, user: dict, scan_doc: dict, scan_type: str) -> str:
    score = scan_doc.get("score", 0)
    note  = (
        scan_doc.get("advice")
        or scan_doc.get("score_recommendation_note")
        or ""
    )
    return (
        f"Step: {step['title']} | Product: {step['product_name']} | "
        f"Slot: {step['time_slot']} | Reason: {step['reason']}\n"
        f"User: {_fmt_profile(user)}\n"
        f"Scan: score {score}/100 — {note}\n"
        "Write the compact JSON description."
    )


# ── Phase 1: metadata call ────────────────────────────────────────────────────

async def _call_meta(prompt: str) -> dict:
    client = ClaudeClient()
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(system=_META_SYSTEM, user=prompt, max_tokens=512),
        )
    except Exception as exc:
        logger.error("Routine meta LLM error: %s", exc)
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            l for l in raw.splitlines() if not l.strip().startswith("```")
        ).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=422,
                    detail=f"AI returned unparseable metadata: {raw[:200]}",
                )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"No JSON in AI response: {raw[:200]}",
            )

    if "needs_update" not in data:
        raise HTTPException(
            status_code=422,
            detail=f"AI metadata missing 'needs_update'. Got: {str(data)[:200]}",
        )
    return data


# ── Phase 2: description call ─────────────────────────────────────────────────

_FALLBACK_DESC = StepDescription(
    what       = "This step targets your detected skin condition with a focused active ingredient.",
    how_to_use = [
        "Cleanse skin and pat dry before applying.",
        "Apply a small amount to the affected area.",
        "Follow with moisturiser and SPF if used in the morning.",
    ],
    tip     = "Introduce gradually — start every other day to assess tolerance.",
    warning = "Discontinue if persistent redness or irritation occurs.",
)


async def _generate_description(
    step: dict,
    user: dict,
    scan_doc: dict,
    scan_type: str,
) -> StepDescription:
    prompt = _build_desc_prompt(step, user, scan_doc, scan_type)
    client = ClaudeClient()
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(system=_DESC_SYSTEM, user=prompt, max_tokens=400),
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(
                l for l in raw.splitlines() if not l.strip().startswith("```")
            ).strip()

        data = json.loads(raw)

        # Validate + coerce
        how = data.get("how_to_use", [])
        if not isinstance(how, list):
            how = [str(how)]
        how = [str(s).strip() for s in how[:4] if s]

        return StepDescription(
            what       = str(data.get("what",    "")).strip() or _FALLBACK_DESC.what,
            how_to_use = how if how else _FALLBACK_DESC.how_to_use,
            tip        = str(data.get("tip",     "")).strip() or _FALLBACK_DESC.tip,
            warning    = str(data.get("warning", "")).strip() or _FALLBACK_DESC.warning,
        )

    except Exception as exc:
        logger.warning(
            "Description generation failed for '%s': %s",
            step.get("product_name"), exc,
        )
        return StepDescription(
            what       = step.get("bio", _FALLBACK_DESC.what),
            how_to_use = _FALLBACK_DESC.how_to_use,
            tip        = _FALLBACK_DESC.tip,
            warning    = _FALLBACK_DESC.warning,
        )


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _steps_col():
    return get_db()["routine_steps"]


async def _fetch_steps(user_id: str) -> list[dict]:
    return await _steps_col().find({"user_id": user_id}).sort("order", 1).to_list(200)


async def _insert_steps(
    user_id:   str,
    steps:     list[dict],
    scan_doc:  dict,
    scan_type: str,
    user:      dict,
) -> list[GeneratedStep]:
    """
    Insert validated step dicts + their Phase 2 descriptions into MongoDB.
    All description calls run concurrently via asyncio.gather.

    product_image_url resolution (priority order):
      Source A (product scan) → scan_doc["product_image_url"] from OBF.
                                 Real product photo when barcode was found.
      Source C (DB catalogue)  → real product from admin product catalogue
                                 matched by detected conditions.
      Source B (fallback)      → _category_icon(product_name).
                                 Generic category icon from _CATEGORY_ICONS map.
    """
    # Phase 2: generate all descriptions in parallel
    descriptions: list[StepDescription] = await asyncio.gather(
        *[_generate_description(s, user, scan_doc, scan_type) for s in steps]
    )

    # Source A: product scan OBF image (barcode scan)
    obf_image = scan_doc.get("product_image_url") or None

    # Source C: try to find a real product from the admin catalogue
    # that targets the scan's detected conditions.
    db_product: dict | None = None
    if not obf_image:
        try:
            db_product = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _pick_db_product(scan_doc)
            )
        except Exception as exc:
            logger.warning("DB product lookup failed (non-fatal): %s", exc)

    saved: list[GeneratedStep] = []
    for step, desc in zip(steps, descriptions):
        time_slot = step.get("time_slot", "morning")
        if time_slot not in ("morning", "night", "weekly"):
            time_slot = "morning"

        last = await _steps_col().find_one(
            {"user_id": user_id, "time_slot": time_slot},
            sort=[("order", -1)],
        )
        next_order = (last["order"] + 1) if last else 0

        title   = str(step.get("title",        "")).strip()[:80]
        product = str(step.get("product_name", "")).strip()[:120]
        bio     = str(step.get("bio",          "")).strip()[:300]
        reason  = str(step.get("reason",       "")).strip()[:200]

        # Resolve image URL with priority: Source A > Source C > Source B
        if obf_image:
            product_image_url = obf_image
            recommended_product_id = None
        elif db_product:
            product_image_url = db_product["image_url"]
            recommended_product_id = db_product["id"]
            # Prefer the real product name when available
            if db_product.get("name"):
                product = db_product["name"][:120]
        else:
            product_image_url = _category_icon(product)
            recommended_product_id = None

        doc = {
            "user_id":                  user_id,
            "time_slot":                time_slot,
            "title":                    title,
            "product_name":             product,
            "bio":                      bio,
            "description":              desc.model_dump(),
            "instructions":             bio,          # backward-compat
            "product_image_url":        product_image_url,
            "recommended_product_id":   recommended_product_id,
            "order":                    next_order,
            "completed_date":           None,
            "created_at":               datetime.now(timezone.utc),
            "ai_generated":             True,
            "ai_reason":                reason,
        }

        result = await _steps_col().insert_one(doc)
        saved.append(GeneratedStep(
            step_id           = str(result.inserted_id),
            time_slot         = time_slot,
            title             = title or product,
            product_name      = product,
            bio               = bio,
            description       = desc,
            reason            = reason,
            product_image_url = product_image_url,
        ))

    return saved


def _fmt_summary(doc: dict) -> RoutineStepSummary:
    raw_desc = doc.get("description")
    desc_obj: Optional[StepDescription] = None
    if isinstance(raw_desc, dict):
        try:
            desc_obj = StepDescription(**raw_desc)
        except Exception:
            pass

    return RoutineStepSummary(
        step_id           = str(doc["_id"]),
        time_slot         = doc.get("time_slot", "morning"),
        title             = doc.get("title") or None,
        product_name      = doc.get("product_name", ""),
        bio               = doc.get("bio") or doc.get("instructions") or None,
        description       = desc_obj,
        product_image_url = doc.get("product_image_url") or None,
    )


# ── Mock data ─────────────────────────────────────────────────────────────────

def _mock_desc_face() -> StepDescription:
    return StepDescription(
        what=(
            "Salicylic acid (BHA) penetrates oil-filled pores to dissolve "
            "the buildup causing your detected acne and enlarged pores."
        ),
        how_to_use=[
            "Wet face with lukewarm water and apply a coin-sized amount.",
            "Massage gently for 30 seconds, leave on 30 s, then rinse.",
            "Follow with lightweight moisturiser and SPF in the morning.",
        ],
        tip="Start once daily at night for the first two weeks to build tolerance.",
        warning="Increase sun sensitivity — always apply SPF 30+ the next morning.",
    )

def _mock_desc_hair() -> StepDescription:
    return StepDescription(
        what=(
            "Antifungal actives (ketoconazole or zinc pyrithione) directly target "
            "the Malassezia yeast driving your detected dandruff and scalp oiliness."
        ),
        how_to_use=[
            "Apply generously to wet scalp and massage for 60 seconds.",
            "Leave on for 2–5 minutes, then rinse thoroughly.",
            "Use twice weekly for 4 weeks, then reduce to once weekly.",
        ],
        tip="Apply to dry hair before wetting for stubborn dandruff — improves contact time.",
        warning="Stop and consult a doctor if burning or significant shedding occurs.",
    )


def _mock_steps_for_scan(scan_type: str) -> tuple[list[dict], list[StepDescription]]:
    if scan_type == "face":
        steps = [
            {
                "time_slot":    "morning",
                "title":        "Salicylic Cleanser",
                "product_name": "Salicylic Acid Cleanser",
                "bio":          "Clears excess oil and unclogs pores every morning.",
                "reason":       "Targets acne and oiliness from your face scan.",
            },
        ]
        descs = [_mock_desc_face()]
    elif scan_type == "hair_scalp":
        steps = [
            {
                "time_slot":    "weekly",
                "title":        "Antifungal Scalp Shampoo",
                "product_name": "Antifungal Scalp Shampoo",
                "bio":          "Reduces dandruff and rebalances scalp twice weekly.",
                "reason":       "Addresses dandruff and scalp oiliness from scan.",
            },
        ]
        descs = [_mock_desc_hair()]
    else:
        steps = [
            {
                "time_slot":    "morning",
                "title":        "Scanned Product Step",
                "product_name": "Scanned Product",
                "bio":          "Apply as directed after cleansing for best results.",
                "reason":       "Incorporated per product instructions.",
            },
        ]
        descs = [_FALLBACK_DESC]

    return steps, descs


# ── Core handler ──────────────────────────────────────────────────────────────

async def _generate_routine(
    scan_id:   str,
    scan_type: str,   # "face" | "hair_scalp" | "product"
    user:      dict,
) -> RoutineGenerateResponse:
    user_id = str(user["_id"])

    # Load scan
    try:
        oid = ObjectId(scan_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scan ID format.")

    db = get_db()
    scan_doc = await db["scan_results"].find_one(
        {"_id": oid, "user_id": user_id, "scan_type": scan_type}
    )
    if not scan_doc:
        raise HTTPException(
            status_code=404,
            detail="Scan not found or does not belong to this user.",
        )

    existing = await _fetch_steps(user_id)

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — mock routine for user %s scan %s", user_id, scan_id)
        mock_steps, mock_descs = _mock_steps_for_scan(scan_type)

        saved: list[GeneratedStep] = []
        for step, desc in zip(mock_steps, mock_descs):
            time_slot = step["time_slot"]
            last = await _steps_col().find_one(
                {"user_id": user_id, "time_slot": time_slot},
                sort=[("order", -1)],
            )
            next_order = (last["order"] + 1) if last else 0
            product_image_url = _category_icon(step["product_name"])
            doc = {
                "user_id":           user_id,
                "time_slot":         time_slot,
                "title":             step["title"],
                "product_name":      step["product_name"],
                "bio":               step["bio"],
                "description":       desc.model_dump(),
                "instructions":      step["bio"],
                "product_image_url": product_image_url,
                "order":             next_order,
                "completed_date":    None,
                "created_at":        datetime.now(timezone.utc),
                "ai_generated":      True,
                "ai_reason":         step["reason"],
            }
            r = await _steps_col().insert_one(doc)
            saved.append(GeneratedStep(
                step_id           = str(r.inserted_id),
                time_slot         = time_slot,
                title             = step["title"],
                product_name      = step["product_name"],
                bio               = step["bio"],
                description       = desc,
                reason            = step["reason"],
                product_image_url = product_image_url,
            ))

        all_after = await _fetch_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = True,
            message         = "Added new steps based on your scan results. Stay consistent!",
            added_steps     = saved,
            existing_steps  = [_fmt_summary(s) for s in all_after],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── Phase 1: metadata ─────────────────────────────────────────────────────
    if scan_type == "product":
        prompt = _build_product_meta_prompt(scan_doc, user, existing)
    else:
        prompt = _build_meta_prompt(scan_doc, scan_type, user, existing)

    data = await _call_meta(prompt)

    # No update needed
    if not data.get("needs_update", True):
        all_steps = await _fetch_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = False,
            message         = str(data.get(
                "message", "Your current routine is well-suited to your scan results."
            )),
            added_steps     = [],
            existing_steps  = [_fmt_summary(s) for s in all_steps],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── BUG FIX: prompt uses "new_step", fall back to "new_steps" if needed ──
    raw_new = data.get("new_step", data.get("new_steps", []))
    if not isinstance(raw_new, list):
        raw_new = []

    valid: list[dict] = []
    for s in raw_new[:5]:
        if not isinstance(s, dict):
            continue
        product = str(s.get("product_name", "")).strip()
        if not product:
            continue
        valid.append({
            "time_slot":    str(s.get("time_slot", "morning")).lower().strip(),
            "title":        str(s.get("title", product)).strip()[:80],
            "product_name": product[:120],
            "bio":          str(s.get("bio", "")).strip()[:300],
            "reason":       str(s.get("reason", "")).strip()[:200],
        })

    if not valid:
        all_steps = await _fetch_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = False,
            message         = "Your current routine covers your scan findings well.",
            added_steps     = [],
            existing_steps  = [_fmt_summary(s) for s in all_steps],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── Phase 2 + save ────────────────────────────────────────────────────────
    saved_steps = await _insert_steps(user_id, valid, scan_doc, scan_type, user)
    all_after   = await _fetch_steps(user_id)

    return RoutineGenerateResponse(
        routine_updated = True,
        message         = str(data.get(
            "message",
            f"Added {len(saved_steps)} new step"
            f"{'s' if len(saved_steps) != 1 else ''} based on your scan results.",
        )),
        added_steps    = saved_steps,
        existing_steps = [_fmt_summary(s) for s in all_after],
        scan_id        = scan_id,
        scan_type      = scan_type,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

_DESC_FOOTER = (
    "\n\n**Description shape** (100-150 words, structured JSON):\n"
    "- `what` — what the product does + why it suits this user\n"
    "- `how_to_use` — 3 plain-text steps\n"
    "- `tip` — 1 expert tip\n"
    "- `warning` — 1 key caution\n\n"
    f"**LLM:** {'🏠 LM Studio' if USE_LOCAL_LLM else '☁️ Anthropic Claude'}"
)


@router.post(
    "/face/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate Routine from Face Scan",
    description    = (
        "Analyses a face scan and adds the single most impactful skincare step. "
        "Respects existing routine, allergens, budget, and life phase."
        + _DESC_FOOTER
    ),
)
async def generate_routine_from_face(
    scan_id: str, current_user: CurrentUser
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "face", current_user)


@router.post(
    "/hair_scalp/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate Routine from Hair & Scalp Scan",
    description    = (
        "Analyses a hair/scalp scan and adds the single most impactful haircare step. "
        "Respects existing routine, allergens, budget, and life phase."
        + _DESC_FOOTER
    ),
)
async def generate_routine_from_hair_scalp(
    scan_id: str, current_user: CurrentUser
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "hair_scalp", current_user)


@router.post(
    "/product/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate Routine Step from Product Scan",
    description    = (
        "Takes a scanned product and generates a step showing how to incorporate "
        "it into the user's existing routine. Flags duplicates and suggests swaps."
        + _DESC_FOOTER
    ),
)
async def generate_routine_from_product(
    scan_id: str, current_user: CurrentUser
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "product", current_user)