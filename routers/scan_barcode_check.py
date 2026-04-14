# routers/scan_barcode_check.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Barcode × Routine Conflict Checker           ║
║                                                                  ║
║  POST /scan/barcode-check                                        ║
║                                                                  ║
║  Pipeline:                                                       ║
║   1. Receive barcode string                                      ║
║   2. Fetch product data from Open Beauty Facts                   ║
║   3. Fetch user profile  (GET /auth/me equivalent)               ║
║   4. Fetch user's active routine steps                           ║
║   5. Fetch latest face scan + hair/scalp scan results            ║
║   6. Send everything to Claude for conflict analysis             ║
║   7. Return structured conflict / positiveness JSON              ║
║                                                                  ║
║  Response shape:                                                 ║
║   {                                                              ║
║     "product_title": str,                                        ║
║     "routine_conflict": "yes" | "no",                            ║
║     "conflict_summary": str | null,     ← if conflict            ║
║     "detected_issues":  [...] | null,   ← if conflict            ║
║     "good_summary":     str | null,     ← if no conflict         ║
║     "overall_positiveness": [...] | null ← if no conflict        ║
║   }                                                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, List, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from routers.auth import _get_current_user
from claude_client import ClaudeClient

logger = logging.getLogger(__name__)

router      = APIRouter(prefix="/scan", tags=["Scan"])
CurrentUser = Annotated[dict, Depends(_get_current_user)]

# ── External API constants ────────────────────────────────────────────────────

_OBF_URL     = "https://world.openbeautyfacts.org/api/v2/product/{}.json"
_OFF_URL     = "https://world.openfoodfacts.org/api/v2/product/{}.json"
_LOOKUP_TIMEOUT = 8.0
_HEADERS     = {"User-Agent": "SkinSense/1.0 (contact@skinsense.app)"}


# ══════════════════════════════════════════════════════════════════════════════
#  Request / Response schemas
# ══════════════════════════════════════════════════════════════════════════════

class BarcodeCheckRequest(BaseModel):
    barcode: str = Field(
        ...,
        min_length=4,
        max_length=30,
        description="EAN-13, UPC-A, EAN-8, or any numeric barcode string.",
    )

    model_config = {"json_schema_extra": {"example": {"barcode": "3600523457441"}}}


class DetectedIssue(BaseModel):
    issued_routine_product_name: str
    issued_summary:              str


class PositiveMatch(BaseModel):
    routine_product_name: str
    summary:              str


class BarcodeCheckResponse(BaseModel):
    product_title:          str
    routine_conflict:       str                       # "yes" | "no"
    # conflict branch
    conflict_summary:       Optional[str]    = None
    detected_issues:        Optional[List[DetectedIssue]]   = None
    # no-conflict branch
    good_summary:           Optional[str]    = None
    overall_positiveness:   Optional[List[PositiveMatch]]   = None


# ══════════════════════════════════════════════════════════════════════════════
#  Data fetchers
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_product(barcode: str) -> dict | None:
    """
    Try Open Beauty Facts first; fall back to Open Food Facts.
    Returns the raw product dict or None.
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_LOOKUP_TIMEOUT) as client:
        for url_tmpl in (_OBF_URL, _OFF_URL):
            try:
                resp = await client.get(url_tmpl.format(barcode))
                data = resp.json()
                if data.get("status") == 1 and "product" in data:
                    logger.info("Product found on %s for barcode %s", url_tmpl, barcode)
                    return data["product"]
            except Exception as exc:
                logger.warning("Barcode lookup error (%s): %s", url_tmpl, exc)
    return None


def _extract_product_info(product: dict) -> dict:
    """Pull the fields SkinSense needs from the raw Open Beauty Facts response."""
    raw_ingredients = (
        product.get("ingredients_text_en") or
        product.get("ingredients_text") or ""
    ).strip()

    ingredient_list = [
        i.strip().lower().rstrip(".")
        for i in raw_ingredients.replace(";", ",").split(",")
        if i.strip()
    ]

    return {
        "barcode":           product.get("code", "").strip(),
        "name":              (product.get("product_name") or product.get("product_name_en", "")).strip(),
        "brand":             product.get("brands", "").strip(),
        "category":          (product.get("categories", "").split(",")[0]).strip(),
        "image_url":         product.get("image_front_url", "").strip(),
        "ingredients":       ingredient_list,
        "raw_ingredients":   raw_ingredients,
        "allergens":         product.get("allergens", "").strip(),
    }


async def _fetch_routine(user_id: str) -> list[dict]:
    """Return all routine steps for the user (name + time_slot only)."""
    col  = get_db()["routine_steps"]
    docs = await col.find({"user_id": user_id}).sort("order", 1).to_list(200)
    return [
        {
            "product_name": doc.get("product_name", ""),
            "time_slot":    doc.get("time_slot", ""),
            "instructions": doc.get("instructions") or "",
        }
        for doc in docs
    ]


async def _fetch_latest_scan(user_id: str, scan_type: str) -> dict | None:
    """
    Fetch the most recent face or hair_and_scalp scan result.
    scan_type: "face" | "hair_and_scalp"
    """
    col = get_db()["scan_results"]
    doc = await col.find_one(
        {"user_id": user_id, "scan_type": scan_type},
        sort=[("scanned_at", -1)],
    )
    if not doc:
        return None
    return {
        "score":                     doc.get("score"),
        "score_recommendation_note": doc.get("score_recommendation_note"),
        "detected_conditions": [
            {
                "condition":   c.get("condition"),
                "detail":      c.get("detail"),
                "seriousness": c.get("seriousness"),
            }
            for c in (doc.get("detected_conditions") or [])
        ],
        "scanned_at": (
            doc["scanned_at"].isoformat()
            if isinstance(doc.get("scanned_at"), datetime)
            else str(doc.get("scanned_at", ""))
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Claude prompt builder
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are a professional dermatologist and cosmetic chemist AI embedded in a skincare app.

Your task:
  Determine whether a newly scanned product CONFLICTS with any products in the user's
  existing skincare/haircare routine, considering their skin profile and recent scan results.

Conflict rules (use ingredient-level reasoning):
  - Active ingredient overlaps that cause over-exfoliation or irritation
    (e.g. two AHA/BHA products, retinol + vitamin C in same slot)
  - Ingredients that neutralise each other
    (e.g. niacinamide + vitamin C, benzoyl peroxide + retinol)
  - pH mismatches that reduce efficacy
  - Allergen triggers from the user's known allergy list
  - Ingredients contraindicated for the user's current phase (e.g. retinol in pregnancy)
  - Comedogenic clashes for oily/acne-prone skin

Positive synergy rules (when no conflict):
  - Complementary hydration layers
  - Ingredients that enhance each other (e.g. niacinamide + zinc, vitamin C + SPF)
  - Ingredients that address detected scan conditions

Return ONLY valid JSON — no markdown, no preamble, no extra text.

Schema when conflict detected:
{
  "product_title": "<product name from the barcode data>",
  "routine_conflict": "yes",
  "conflict_summary": "<exactly 1 sentence, 10-15 words, specific to the conflict>",
  "detected_issues": [
    {
      "issued_routine_product_name": "<routine product name>",
      "issued_summary": "<one line: which ingredient in the routine product conflicts with which ingredient in the scanned product>"
    }
  ]
}

Schema when NO conflict detected:
{
  "product_title": "<product name from the barcode data>",
  "routine_conflict": "no",
  "good_summary": "<exactly 1 sentence, 10-15 words, positive and specific>",
  "overall_positiveness": [
    {
      "routine_product_name": "<routine product name>",
      "summary": "<one line: which ingredient combination benefits the user>"
    }
  ]
}

Rules:
  - detected_issues: list only REAL conflicts (1-4 items). Do NOT fabricate.
  - overall_positiveness: list 2-4 synergy pairs. If no routine exists, list 0.
  - Summaries: 1 sentence, specific, ingredient-focused, no brand names.
  - Return ONLY the JSON object. Nothing else.
""".strip()


def _build_user_prompt(
    product_info: dict,
    user:         dict,
    routine:      list[dict],
    face_scan:    dict | None,
    scalp_scan:   dict | None,
) -> str:
    """Assemble the full context block sent to Claude."""

    # ── Product ──────────────────────────────────────────────────────────────
    product_block = (
        f"SCANNED PRODUCT:\n"
        f"  Name       : {product_info.get('name') or 'Unknown'}\n"
        f"  Brand      : {product_info.get('brand') or 'Unknown'}\n"
        f"  Category   : {product_info.get('category') or 'Unknown'}\n"
        f"  Allergens  : {product_info.get('allergens') or 'none listed'}\n"
        f"  Ingredients: {product_info.get('raw_ingredients') or 'not available'}\n"
    )

    # ── User profile ─────────────────────────────────────────────────────────
    user_block = (
        f"\nUSER PROFILE:\n"
        f"  Skin type     : {user.get('skin_type') or 'not specified'}\n"
        f"  Skin concerns : {', '.join(user.get('skin_concerns') or []) or 'none'}\n"
        f"  Hair type     : {user.get('hair_type') or 'not specified'}\n"
        f"  Hair concerns : {', '.join(user.get('hair_concerns') or []) or 'none'}\n"
        f"  Allergies     : {', '.join(user.get('allergies') or []) or 'none'}\n"
        f"  Current phase : {(user.get('current_phase') or 'none').replace('_', ' ')}\n"
    )

    # ── Routine ───────────────────────────────────────────────────────────────
    if routine:
        routine_lines = "\n".join(
            f"  [{r['time_slot'].upper()}] {r['product_name']}"
            + (f" — {r['instructions']}" if r.get("instructions") else "")
            for r in routine
        )
        routine_block = f"\nACTIVE ROUTINE STEPS:\n{routine_lines}\n"
    else:
        routine_block = "\nACTIVE ROUTINE STEPS: none added yet\n"

    # ── Recent scans ─────────────────────────────────────────────────────────
    def _fmt_scan(label: str, scan: dict | None) -> str:
        if not scan:
            return f"\nRECENT {label} SCAN: not available\n"
        conds = "; ".join(
            f"{c['condition']} ({c['seriousness']})"
            for c in scan.get("detected_conditions", [])
        )
        return (
            f"\nRECENT {label} SCAN (score {scan.get('score', 'N/A')}/100, "
            f"as of {scan.get('scanned_at', 'unknown')}):\n"
            f"  Note       : {scan.get('score_recommendation_note', '')}\n"
            f"  Conditions : {conds or 'none'}\n"
        )

    face_block  = _fmt_scan("FACE", face_scan)
    scalp_block = _fmt_scan("HAIR/SCALP", scalp_scan)

    return (
        product_block
        + user_block
        + routine_block
        + face_block
        + scalp_block
        + "\nBased on all the above, analyse ingredient-level conflicts between the "
          "scanned product and each routine step. Return ONLY the JSON."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Claude response parser
# ══════════════════════════════════════════════════════════════════════════════

def _parse_claude_response(raw: str, fallback_title: str) -> BarcodeCheckResponse:
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=422,
                    detail=f"AI returned unparseable JSON: {raw[:300]}",
                )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"AI returned unparseable JSON: {raw[:300]}",
            )

    conflict = str(data.get("routine_conflict", "no")).strip().lower()
    title    = str(data.get("product_title", fallback_title)).strip()

    if conflict == "yes":
        raw_issues = data.get("detected_issues") or []
        issues = [
            DetectedIssue(
                issued_routine_product_name=str(i.get("issued_routine_product_name", "Unknown")).strip(),
                issued_summary=str(i.get("issued_summary", "")).strip(),
            )
            for i in raw_issues
            if isinstance(i, dict)
        ]
        return BarcodeCheckResponse(
            product_title    = title,
            routine_conflict = "yes",
            conflict_summary = str(data.get("conflict_summary", "")).strip() or None,
            detected_issues  = issues or None,
        )
    else:
        raw_positives = data.get("overall_positiveness") or []
        positives = [
            PositiveMatch(
                routine_product_name=str(p.get("routine_product_name", "Unknown")).strip(),
                summary=str(p.get("summary", "")).strip(),
            )
            for p in raw_positives
            if isinstance(p, dict)
        ]
        return BarcodeCheckResponse(
            product_title        = title,
            routine_conflict     = "no",
            good_summary         = str(data.get("good_summary", "")).strip() or None,
            overall_positiveness = positives or None,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Route
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/barcode-check",
    response_model = BarcodeCheckResponse,
    summary        = "Barcode × Routine Conflict Checker",
    description    = (
        "Submit a product barcode to check if the product conflicts with your "
        "active skincare/haircare routine.\n\n"
        "**What this endpoint does:**\n"
        "1. Fetches full product + ingredient data from Open Beauty Facts\n"
        "2. Loads your profile (skin type, concerns, allergies, phase)\n"
        "3. Loads all your active routine steps\n"
        "4. Loads your latest face scan + hair/scalp scan results\n"
        "5. Sends everything to Claude for ingredient-level conflict analysis\n\n"
        "**Response when `routine_conflict = yes`:**\n"
        "```json\n"
        "{\n"
        '  "product_title": "...",\n'
        '  "routine_conflict": "yes",\n'
        '  "conflict_summary": "one sentence 10-15 words",\n'
        '  "detected_issues": [\n'
        '    { "issued_routine_product_name": "...", "issued_summary": "..." }\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "**Response when `routine_conflict = no`:**\n"
        "```json\n"
        "{\n"
        '  "product_title": "...",\n'
        '  "routine_conflict": "no",\n'
        '  "good_summary": "one sentence 10-15 words",\n'
        '  "overall_positiveness": [\n'
        '    { "routine_product_name": "...", "summary": "..." }\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "Set `MOCK_MODE=true` in `.env` to skip all external calls."
    ),
    responses={
        400: {"description": "Barcode not found in any database"},
        422: {"description": "Claude returned malformed data"},
        502: {"description": "AI or external API error"},
    },
)
async def barcode_check(
    payload:      BarcodeCheckRequest,
    current_user: CurrentUser,
) -> BarcodeCheckResponse:
    barcode = payload.barcode.strip()
    user_id = str(current_user["_id"])

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — returning mock barcode check for '%s'", barcode)
        return BarcodeCheckResponse(
            product_title    = "Neutrogena Hydro Boost Water Gel",
            routine_conflict = "no",
            good_summary     = "This lightweight gel layers well with your existing hydration routine.",
            overall_positiveness = [
                PositiveMatch(
                    routine_product_name = "Vitamin C Serum",
                    summary = "Hyaluronic acid in this gel boosts the absorption of your vitamin C serum.",
                ),
                PositiveMatch(
                    routine_product_name = "Niacinamide Toner",
                    summary = "Both products share glycerin, doubling your skin's moisture retention.",
                ),
            ],
        )

    # ── Fetch product from Open Beauty Facts ─────────────────────────────────
    logger.info("Fetching barcode data for: %s", barcode)
    raw_product = await _fetch_product(barcode)

    if not raw_product:
        raise HTTPException(
            status_code = 400,
            detail = (
                f"Product with barcode '{barcode}' was not found in Open Beauty Facts "
                f"or Open Food Facts. The product may not be in the database yet."
            ),
        )

    product_info = _extract_product_info(raw_product)
    product_title = product_info.get("name") or f"Product ({barcode})"

    # ── Fetch user routine + scan results concurrently ───────────────────────
    routine, face_scan, scalp_scan = await asyncio.gather(
        _fetch_routine(user_id),
        _fetch_latest_scan(user_id, "face"),
        _fetch_latest_scan(user_id, "hair_and_scalp"),
    )

    logger.info(
        "Context loaded — routine steps: %d, face scan: %s, scalp scan: %s",
        len(routine),
        "yes" if face_scan else "no",
        "yes" if scalp_scan else "no",
    )

    # ── Build Claude prompt ───────────────────────────────────────────────────
    user_prompt = _build_user_prompt(
        product_info = product_info,
        user         = current_user,
        routine      = routine,
        face_scan    = face_scan,
        scalp_scan   = scalp_scan,
    )

    # ── Call Claude (run in executor to keep endpoint async) ─────────────────
    client = ClaudeClient()
    try:
        raw_response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = _SYSTEM_PROMPT,
                user       = user_prompt,
                max_tokens = 1024,
            ),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("Claude error in barcode-check: %s", exc)
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    if not raw_response or not raw_response.strip():
        raise HTTPException(status_code=502, detail="AI returned an empty response.")

    logger.debug("Claude raw response: %s", raw_response[:400])

    # ── Parse + return ────────────────────────────────────────────────────────
    return _parse_claude_response(raw_response, product_title)