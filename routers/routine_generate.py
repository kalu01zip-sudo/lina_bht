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
║  Each generated step includes:                                  ║
║   title       → 2-4 words (linked to product name)             ║
║   bio         → 10-15 words one-line routine summary            ║
║   description → Full HTML (~2 pages): how to apply, pros/cons, ║
║                 recommended products, usage tips, warnings      ║
║                                                                  ║
║  Routine is divided into three sections:                        ║
║   morning | night | weekly                                      ║
║                                                                  ║
║  How it works (TWO-PHASE GENERATION):                           ║
║   Phase 1 — Metadata call (lightweight JSON, no description):   ║
║     Claude returns needs_update, message, and step metadata     ║
║     (time_slot / title / product_name / bio / reason) only.    ║
║     JSON stays small (<1 KB) → 100% reliable parsing.          ║
║                                                                  ║
║   Phase 2 — Description call (plain HTML, no JSON wrapping):   ║
║     For each step, Claude generates the full HTML description   ║
║     as raw text. No JSON escaping required → never breaks.      ║
║     All steps are generated concurrently (asyncio.gather).      ║
║                                                                  ║
║  WHY TWO PHASES?                                                 ║
║   Embedding 600+ word HTML in a JSON string was the root cause  ║
║   of all "unparseable response" errors:                         ║
║     1. Token truncation: 4096 tokens is too small for 5 steps   ║
║        × 600+ words of HTML → JSON cut off mid-string.         ║
║     2. JSON escape failures: HTML quotes inside href="..." etc  ║
║        must be escaped as \" in JSON. Claude forgets this,      ║
║        producing invalid JSON that even regex fallback can't fix.║
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

from claude_client import ClaudeClient, USE_LOCAL_LLM
from database import get_db
from routers.auth import _get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scan", tags=["Scan"])

CurrentUser = Annotated[dict, Depends(_get_current_user)]
TimeSlot    = Literal["morning", "night", "weekly"]


# ── Schemas ───────────────────────────────────────────────────────────────────

class GeneratedStep(BaseModel):
    """A single routine step as returned by Claude and saved to DB."""
    step_id:      str
    time_slot:    TimeSlot
    title:        str           # 2-4 words, linked to product name
    product_name: str
    bio:          str           # 10-15 words one-line summary
    description:  str           # Full HTML: how to apply, pros/cons, recommended products
    reason:       str           # why this step was added (shown in app UI)


class RoutineStepSummary(BaseModel):
    """Summary of an existing step (for the response overview)."""
    step_id:      str
    time_slot:    TimeSlot
    title:        Optional[str]
    product_name: str
    bio:          Optional[str]
    description:  Optional[str]


class RoutineGenerateResponse(BaseModel):
    routine_updated: bool
    message:         str
    added_steps:     List[GeneratedStep]
    existing_steps:  List[RoutineStepSummary]   # all steps incl. newly added
    scan_id:         str
    scan_type:       str


# ── Phase 1 system prompt — metadata only, NO description field ───────────────
#
# CRITICAL: This prompt deliberately omits the description field from the
# JSON schema. Keeping descriptions out of JSON eliminates both token
# truncation and HTML-escape issues that caused unparseable responses.

_ROUTINE_META_SYSTEM = """\
You are a professional skincare and haircare routine advisor embedded in a mobile app.
Analyse the user's scan results and existing care routine, then decide if new steps
should be added to improve their skin or hair health.

Return ONLY a valid JSON object — no markdown, no code fences, no extra text.

OPTION A — Existing routine is sufficient (no changes needed):
{
  "needs_update": false,
  "message": "<1 sentence: friendly confirmation that their current routine is on track>"
}

OPTION B — New steps should be added:
{
  "needs_update": true,
  "message": "<1 sentence: what you're adding and why, warm and encouraging tone>",
  "new_steps": [
    {
      "time_slot":    "<morning|night|weekly>",
      "title":        "<2-4 words tightly linked to the product name, e.g. 'Niacinamide Pore Serum', 'Salicylic Cleanser Step', 'Clay Detox Mask'>",
      "product_name": "<specific product type, 2-6 words, generic — no brand names>",
      "bio":          "<10-15 words: one-line routine summary, present tense, action-focused>",
      "reason":       "<why this addresses the scan finding, 8-12 words>"
    }
  ]
}

RULES:
- new_steps   : only 1 step. Prioritise the highest-severity conditions.
- No duplicates: do NOT suggest any product already present in the existing routine.
- Allergens   : if the user profile lists known allergens, NEVER suggest products
                containing them.
- Budget      : match product tier to the user's budget preference
                (budget_friendly → drugstore; midrange → mid-range; premium → luxury).
- Pregnancy   : if current_phase is pregnant, avoid retinoids, high-dose salicylic acid,
                benzoyl peroxide, or essential oils.
- time_slot   : assign thoughtfully (vitamin C → morning; retinol → night;
                clay mask → weekly; SPF → morning only).
- product_name: use category names, not brands
                (e.g. "Salicylic Acid Cleanser", "Niacinamide Serum", "Clay Mask").
- title       : must be 2-4 words and directly reflect the product name.
- bio         : exactly 10-15 words, one sentence, present tense, action-focused.
- Return ONLY the JSON. Nothing else. Do NOT include a description field.
""".strip()


# ── Phase 2 system prompt — pure HTML description, no JSON ───────────────────
#
# Returning raw HTML (not JSON-embedded) means no escaping issues at all.
# Claude writes the HTML directly; we store it as-is.

_STEP_DESC_SYSTEM = """\
You are a professional skincare and haircare advisor writing a detailed usage guide
for a single routine step inside a mobile app.

Return ONLY the HTML content — no JSON, no markdown code fences, no preamble,
no "Here is the HTML:" opener. Start directly with the first <h2> tag.

The HTML must cover ALL of the following sections in this order:

<h2>About This Step</h2>
  2-3 paragraphs explaining what this product type does, its active ingredients,
  and why it was chosen for this user's specific scan findings.

<h2>How to Apply</h2>
  Numbered step-by-step instructions using <ol><li> tags. Include:
  - Skin/scalp prep before application
  - Exact application method (amount, technique, direction)
  - Wait time or layering order
  - How to finish / lock in (e.g. follow with moisturiser)
  - Frequency (daily, twice daily, 2-3x per week, etc.)

<h2>Pros &amp; Cons</h2>
  Two sub-sections:
  <h3>&#x2705; Benefits</h3>   — at least 4 bullet points using <ul><li>
  <h3>&#x26A0;&#xFE0F; Considerations</h3> — at least 3 honest caveats using <ul><li>

<h2>Recommended Products</h2>
  3-4 specific GENERIC product types / ingredient formulations to look for
  (no brand names). For each: name, key ingredient, why it suits this profile.
  Use <ul><li>. Match budget preference.

<h2>Pro Tips</h2>
  3-5 expert tips specific to this product and the user's conditions.
  Use <ul><li>.

<h2>Warnings &amp; When to Stop</h2>
  Precautions, patch-test advice, and signs the product is not working.
  Include a pregnancy-safe note if the user's phase is pregnant or postpartum.

HTML rules:
  - Allowed tags ONLY: <h2> <h3> <p> <ul> <ol> <li> <strong> <em> <br>
  - No <html> <head> <body> <div> <style> wrappers
  - No inline CSS, no class or id attributes
  - Use &amp; for & in text — never a bare ampersand outside a tag
  - 100-200 words of content
  - Be specific and relevant to the user's actual profile and scan findings
""".strip()


# ── Prompt builders ────────────────────────────────────────────────────────────

def _format_existing_routine(steps: list[dict]) -> str:
    if not steps:
        return "  (no steps yet — this user has no existing routine)"

    by_slot: dict[str, list[str]] = {"morning": [], "night": [], "weekly": []}
    for s in steps:
        slot = s.get("time_slot", "morning")
        if slot not in by_slot:
            continue
        name = s.get("product_name", "Unknown")
        inst = s.get("instructions") or s.get("bio") or ""
        by_slot[slot].append(f"    • {name}" + (f" — {inst}" if inst else ""))

    lines = []
    for slot, items in by_slot.items():
        lines.append(f"  {slot.capitalize()} ({len(items)} step{'s' if len(items) != 1 else ''}):")
        lines.extend(items if items else ["    (empty)"])
    return "\n".join(lines)


def _format_profile(user: dict) -> str:
    parts = []
    if user.get("skin_type"):
        parts.append(f"Skin type: {user['skin_type']}")
    if user.get("hair_type"):
        parts.append(f"Hair type: {user['hair_type'].replace('_', ' ')}")
    if user.get("current_phase"):
        parts.append(f"Hormonal / life phase: {user['current_phase'].replace('_', ' ')}")

    skin_concerns = user.get("skin_concerns") or []
    if skin_concerns:
        parts.append(f"Skin concerns: {', '.join(skin_concerns)}")
    hair_concerns = user.get("hair_concerns") or []
    if hair_concerns:
        parts.append(f"Hair concerns: {', '.join(hair_concerns)}")

    allergies = user.get("allergies") or []
    if allergies:
        parts.append(f"Known allergens (NEVER recommend these): {', '.join(allergies)}")

    if user.get("budget"):
        parts.append(f"Budget preference: {user['budget'].replace('_', ' ')}")

    return "\n  ".join(parts) if parts else "No profile data."


def _build_scan_prompt(
    scan_doc:       dict,
    scan_type:      str,
    user:           dict,
    existing_steps: list[dict],
) -> str:
    """
    Build the Phase 1 metadata prompt for face / hair_scalp scans.
    Supports both field-name variants:
      • scan_face / scan_hair_scalp: advice + detected_triggers
      • ClaudeVisionAnalyzer: score_recommendation_note + detected_conditions
    """
    score = scan_doc.get("score", 0)

    note = (
        scan_doc.get("advice")
        or scan_doc.get("score_recommendation_note")
        or ""
    )

    raw_triggers   = scan_doc.get("detected_triggers", [])
    raw_conditions = scan_doc.get("detected_conditions", [])

    condition_lines_parts = []
    for t in raw_triggers:
        if not isinstance(t, dict):
            continue
        name   = t.get("trigger_name", "Unknown")
        level  = t.get("trigger_level", "unknown")
        advice = t.get("cure_advice", "")
        condition_lines_parts.append(f"    • {name} ({level} severity) — {advice}")

    for c in raw_conditions:
        if not isinstance(c, dict):
            continue
        name    = c.get("condition", "Unknown")
        serious = c.get("seriousness", "unknown")
        detail  = c.get("detail", "")
        condition_lines_parts.append(f"    • {name} ({serious} severity) — {detail}")

    condition_lines = "\n".join(condition_lines_parts) or "    (no conditions detected)"
    label = "face" if scan_type == "face" else "hair/scalp"

    return (
        f"SCAN RESULTS ({label} scan, score: {score}/100):\n"
        f"  Overall recommendation: {note}\n"
        f"  Detected conditions:\n{condition_lines}\n\n"
        f"USER PROFILE:\n  {_format_profile(user)}\n\n"
        f"EXISTING ROUTINE:\n{_format_existing_routine(existing_steps)}\n\n"
        f"Based on the scan findings and existing routine, decide if new steps are needed.\n"
        f"Return the JSON (step metadata only — no description field)."
    )


def _build_product_prompt(
    scan_doc:       dict,
    user:           dict,
    existing_steps: list[dict],
) -> str:
    """Phase 1 prompt for product scan — focus on how to use this product."""
    product_name = scan_doc.get("product_name", "this product")
    brand        = scan_doc.get("brand")
    best_use     = scan_doc.get("best_use", "")
    how_to_apply = scan_doc.get("how_to_apply", "")
    ingredients  = scan_doc.get("ingredients", [])
    side_effects = scan_doc.get("side_effects", "")

    product_label = f"{brand} {product_name}" if brand else product_name
    ing_str       = ", ".join(ingredients[:8]) if ingredients else "not specified"

    return (
        f"SCANNED PRODUCT: {product_label}\n"
        f"  Best use: {best_use}\n"
        f"  How to apply: {how_to_apply}\n"
        f"  Key ingredients: {ing_str}\n"
        f"  Side effects / warnings: {side_effects or 'none noted'}\n\n"
        f"USER PROFILE:\n  {_format_profile(user)}\n\n"
        f"EXISTING ROUTINE:\n{_format_existing_routine(existing_steps)}\n\n"
        "Generate routine step metadata (time_slot, title, product_name, bio, reason) "
        "showing how to incorporate this product into the user's existing routine.\n"
        "Avoid duplicating any existing steps.\n"
        "If the user already has an equivalent product, suggest a swap instead.\n"
        "Return the JSON (step metadata only — no description field)."
    )


def _extract_scan_context(scan_doc: dict, scan_type: str) -> str:
    """
    Build a short plain-text scan context string for Phase 2 description prompts.
    Gives Claude enough context to write a relevant HTML description without
    repeating the full scan JSON.
    """
    score = scan_doc.get("score", 0)
    note  = (
        scan_doc.get("advice")
        or scan_doc.get("score_recommendation_note")
        or ""
    )

    conditions: list[str] = []
    for t in scan_doc.get("detected_triggers", []):
        if isinstance(t, dict):
            name  = t.get("trigger_name", "")
            level = t.get("trigger_level", "")
            if name:
                conditions.append(f"{name} ({level})")
    for c in scan_doc.get("detected_conditions", []):
        if isinstance(c, dict):
            name    = c.get("condition", "")
            serious = c.get("seriousness", "")
            if name:
                conditions.append(f"{name} ({serious})")

    label    = "face" if scan_type == "face" else "hair/scalp" if scan_type == "hair_scalp" else "product"
    cond_str = ", ".join(conditions) or "no specific conditions detected"

    return (
        f"{label} scan — score {score}/100. "
        f"Summary: {note} "
        f"Detected conditions: {cond_str}."
    )


# ── Phase 1: metadata call ────────────────────────────────────────────────────

async def _call_claude_meta(system: str, user_prompt: str) -> dict:
    """
    Phase 1 — get routine step metadata (no description field).

    The JSON returned here is small (<1 KB even with 5 steps) because
    descriptions are excluded. This makes parsing 100% reliable.

    max_tokens=1024 is more than sufficient for 5 steps of metadata.
    """
    client = ClaudeClient()

    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = system,
                user       = user_prompt,
                max_tokens = 1024,
            ),
        )
    except Exception as exc:
        logger.error("Routine metadata LLM error: %s", exc)
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    if not raw:
        raise HTTPException(status_code=502, detail="AI returned an empty response.")

    raw = raw.strip()

    # Strip accidental markdown fences (```json ... ```)
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    # Primary parse
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: extract the outermost JSON object.
        # This is safe here because there is NO embedded HTML (no nested quotes).
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                logger.error("Unparseable metadata response: %s", raw[:400])
                raise HTTPException(
                    status_code=422,
                    detail=f"AI returned unparseable metadata JSON: {raw[:300]}",
                )
        else:
            logger.error("No JSON object in metadata response: %s", raw[:400])
            raise HTTPException(
                status_code=422,
                detail=f"AI returned no JSON object: {raw[:300]}",
            )

    if "needs_update" not in data:
        raise HTTPException(
            status_code=422,
            detail=f"AI metadata response missing 'needs_update'. Got: {str(data)[:200]}",
        )

    return data


# ── Phase 2: description call ─────────────────────────────────────────────────

async def _generate_step_description(
    step:         dict,
    user:         dict,
    scan_context: str,
) -> str:
    """
    Phase 2 — generate the full HTML description for a single routine step.

    Returns a raw HTML string. Because we are NOT wrapping this in JSON,
    Claude never needs to escape any HTML characters, which was the root
    cause of all parse failures in the original single-phase approach.

    Falls back to a minimal <p> element on any error so the step is always
    saved with some content.
    """
    title   = step.get("title") or step.get("product_name", "Product")
    product = step.get("product_name", "")
    slot    = step.get("time_slot", "morning")
    reason  = step.get("reason") or step.get("bio", "")

    prompt = (
        f"Routine step: {title}\n"
        f"Product category: {product}\n"
        f"Time slot: {slot}\n"
        f"Why this step was added: {reason}\n\n"
        f"User profile:\n  {_format_profile(user)}\n\n"
        f"Scan context:\n  {scan_context}\n\n"
        "Write the full HTML description for this routine step.\n"
        "Start directly with <h2>About This Step</h2> — no preamble."
    )

    client = ClaudeClient()
    try:
        html = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(
                system     = _STEP_DESC_SYSTEM,
                user       = prompt,
                max_tokens = 2048,   # ample for 600+ words of HTML per step
            ),
        )
        html = html.strip()
        # Strip any accidental markdown fences
        if html.startswith("```"):
            html = "\n".join(
                line for line in html.splitlines()
                if not line.strip().startswith("```")
            ).strip()
        return html if html else f"<p>{step.get('bio', '')}</p>"

    except Exception as exc:
        logger.warning(
            "Description generation failed for step '%s': %s",
            product, exc,
        )
        # Fallback: use bio as minimal content rather than crashing
        bio = step.get("bio", "")
        return f"<p>{bio}</p>" if bio else "<p>Follow the application instructions on the product packaging.</p>"


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _steps_col():
    return get_db()["routine_steps"]

def _today() -> str:
    return date.today().isoformat()


async def _fetch_routine_steps(user_id: str) -> list[dict]:
    """Fetch all routine steps for the user, sorted by order."""
    return await _steps_col().find({"user_id": user_id}).sort("order", 1).to_list(200)


async def _insert_steps(
    user_id:   str,
    new_steps: list[dict],
) -> list[GeneratedStep]:
    """
    Insert Claude-suggested steps into the routine_steps collection.
    Returns a list of GeneratedStep (with the assigned MongoDB _id as step_id).
    Stores title, bio, description, and instructions (bio alias for legacy compat).
    """
    saved: list[GeneratedStep] = []

    for step in new_steps:
        time_slot = step.get("time_slot", "morning")
        if time_slot not in ("morning", "night", "weekly"):
            time_slot = "morning"

        # Determine next order in this time_slot
        last = await _steps_col().find_one(
            {"user_id": user_id, "time_slot": time_slot},
            sort=[("order", -1)],
        )
        next_order = (last["order"] + 1) if last else 0

        title        = str(step.get("title",        "")).strip()[:80]
        product      = str(step.get("product_name", "")).strip()[:120]
        bio          = str(step.get("bio",          "")).strip()[:300]
        description  = str(step.get("description",  "")).strip()   # full HTML — no length cap
        instructions = bio   # populate instructions from bio for backward-compat with routine.py

        doc = {
            "user_id":        user_id,
            "time_slot":      time_slot,
            "title":          title,
            "product_name":   product,
            "bio":            bio,
            "description":    description,
            "instructions":   instructions,
            "order":          next_order,
            "completed_date": None,
            "created_at":     datetime.now(timezone.utc),
            "ai_generated":   True,
            "ai_reason":      str(step.get("reason", "")).strip()[:200],
        }

        result = await _steps_col().insert_one(doc)

        saved.append(GeneratedStep(
            step_id      = str(result.inserted_id),
            time_slot    = time_slot,
            title        = title or product,   # fallback to product_name if title empty
            product_name = product,
            bio          = bio,
            description  = description,
            reason       = doc["ai_reason"],
        ))

    return saved


def _fmt_summary(doc: dict) -> RoutineStepSummary:
    """Map a DB document to RoutineStepSummary. Handles both old and new field schemas."""
    return RoutineStepSummary(
        step_id      = str(doc["_id"]),
        time_slot    = doc.get("time_slot", "morning"),
        title        = doc.get("title") or None,
        product_name = doc.get("product_name", ""),
        bio          = doc.get("bio") or doc.get("instructions") or None,
        description  = doc.get("description") or None,
    )


# ── Mock routine generation ───────────────────────────────────────────────────

_MOCK_HTML_FACE = """\
<h2>About This Step</h2>
<p>A <strong>Salicylic Acid Cleanser</strong> is a beta-hydroxy acid (BHA) based face wash
specifically formulated to penetrate oil-filled pores and dissolve the buildup of dead skin
cells that leads to blackheads, whiteheads, and active breakouts. Salicylic acid (typically
0.5%–2%) is oil-soluble, allowing it to cut through sebum and clear congestion at the source.</p>
<p>Based on your scan results showing mild acne and oiliness, this cleanser directly addresses
the root cause by regulating sebum production and preventing future breakouts from forming.</p>
<p>Used consistently twice daily, it reduces pore size visibility and creates a cleaner canvas
for the serums and moisturisers that follow in your routine.</p>

<h2>How to Apply</h2>
<ol>
  <li>Wet your face with <strong>lukewarm water</strong> — avoid hot water as it strips the skin barrier.</li>
  <li>Dispense a <strong>coin-sized amount</strong> (approximately 1 ml) onto your fingertips.</li>
  <li>Work into a lather between your palms, then <strong>massage gently onto your face in circular motions</strong> for 30–60 seconds. Focus on the T-zone and any active breakout areas.</li>
  <li>Allow the cleanser to <strong>sit on the skin for 30 seconds</strong> before rinsing to maximise BHA contact time.</li>
  <li>Rinse thoroughly with lukewarm water and <strong>pat dry</strong> with a clean towel — do not rub.</li>
  <li>Follow immediately with your toner or serum while skin is still slightly damp.</li>
  <li>Use <strong>morning and night</strong>. If irritation occurs, reduce to once daily (night only) for the first two weeks.</li>
</ol>

<h2>Pros &amp; Cons</h2>
<h3>&#x2705; Benefits</h3>
<ul>
  <li>Dissolves excess sebum and unclogs pores at a deeper level than physical scrubs.</li>
  <li>Reduces active breakouts and prevents new ones from forming.</li>
  <li>Gentle enough for daily use when formulated at 0.5%–1% concentration.</li>
  <li>Doubles as a mild exfoliant, smoothing skin texture over time.</li>
  <li>Non-abrasive — suitable for inflamed or sensitive acne-prone skin.</li>
</ul>
<h3>&#x26A0;&#xFE0F; Considerations</h3>
<ul>
  <li>Can cause initial <em>purging</em> (temporary increase in breakouts) for the first 2–4 weeks as pores clear.</li>
  <li>May cause dryness or flaking — pair with a lightweight, non-comedogenic moisturiser.</li>
  <li>Increases photosensitivity — always follow morning use with SPF 30+.</li>
  <li>Not recommended during pregnancy without dermatologist guidance.</li>
</ul>

<h2>Recommended Products</h2>
<ul>
  <li><strong>0.5% Salicylic Acid Gel Cleanser</strong> — look for gentle BHA formulas with soothing ingredients like aloe vera or green tea. Ideal for daily use on sensitive-acne skin.</li>
  <li><strong>2% BHA Foaming Cleanser</strong> — higher-strength option for moderate acne and oily skin; use every other day initially.</li>
  <li><strong>Salicylic Acid + Niacinamide Cleanser</strong> — combined formula that simultaneously exfoliates and reduces redness. Great mid-range pick.</li>
  <li><strong>Micellar-BHA Hybrid Cleanser</strong> — a two-in-one option for those who prefer a single cleansing step with no rinse required.</li>
</ul>

<h2>Pro Tips</h2>
<ul>
  <li>Start with a <strong>patch test</strong> on your jawline for 3 days before using all over the face.</li>
  <li>If you wear heavy sunscreen or makeup, <strong>double cleanse</strong>: micellar water first, then the salicylic cleanser second.</li>
  <li>Do not use simultaneously with a separate <strong>retinol or AHA</strong> step on the same night — alternate days to prevent over-exfoliation.</li>
  <li>Store the cleanser in a <strong>cool, dry location</strong> — avoid keeping it in a hot shower as heat degrades BHAs over time.</li>
  <li>If you notice persistent peeling after week 3, cut back to <strong>once daily (night only)</strong> and increase your moisturiser.</li>
</ul>

<h2>Warnings &amp; When to Stop</h2>
<p>Discontinue use and consult a dermatologist if you experience <strong>severe redness, swelling, or burning</strong> that persists more than 10 minutes after rinsing. A mild tingling sensation during the first week is normal.</p>
<p>If you are currently using <strong>prescription acne treatments</strong> (e.g. tretinoin, adapalene, clindamycin), check with your dermatologist before adding a BHA cleanser — combining actives can over-strip the barrier.</p>
<p><strong>Pregnancy note:</strong> Low-concentration salicylic acid in rinse-off formulas is generally considered low-risk, but always consult your healthcare provider before introducing new actives during pregnancy.</p>
"""

_MOCK_HTML_HAIR = """\
<h2>About This Step</h2>
<p>An <strong>Antifungal Scalp Shampoo</strong> typically contains <strong>ketoconazole, zinc pyrithione, or selenium sulphide</strong> — active ingredients that directly target the <em>Malassezia</em> yeast overgrowth responsible for dandruff, scalp itching, and flaking. Your scan detected moderate dandruff and oily scalp, which is the most common presentation of Malassezia-related seborrhoeic dermatitis.</p>
<p>Unlike regular shampoos, antifungal formulas work as a treatment — not just a cleanser — and need adequate contact time on the scalp to be effective. Used consistently over 4–6 weeks, most users see a significant reduction in visible flaking and scalp irritation.</p>
<p>This step targets the root cause of your scalp condition rather than just masking the symptoms, which makes it the single most impactful addition to your current routine.</p>

<h2>How to Apply</h2>
<ol>
  <li>Wet hair and scalp thoroughly with <strong>warm (not hot) water</strong>.</li>
  <li>Apply a <strong>generous amount</strong> directly to the scalp — approximately 10–15 ml for medium-length hair.</li>
  <li>Use your fingertips (not nails) to <strong>massage the shampoo into the scalp</strong> for at least 60 seconds, ensuring full coverage across all areas.</li>
  <li><strong>Leave in place for 2–5 minutes</strong> before rinsing — this contact time is essential for the antifungal actives to work.</li>
  <li>Rinse <strong>thoroughly</strong> — any residue left on the scalp can cause further irritation.</li>
  <li>Follow with a lightweight, scalp-friendly conditioner on the <strong>lengths and ends only</strong> — avoid applying conditioner to the scalp.</li>
  <li>Use <strong>twice weekly for 4 weeks</strong>, then reduce to once weekly as a maintenance dose.</li>
</ol>

<h2>Pros &amp; Cons</h2>
<h3>&#x2705; Benefits</h3>
<ul>
  <li>Directly addresses the fungal cause of dandruff — not just a cosmetic fix.</li>
  <li>Reduces scalp oiliness by regulating the sebum-feeding yeast population.</li>
  <li>Measurable improvement typically visible within 2–3 uses.</li>
  <li>Available without prescription in most markets at effective concentrations.</li>
  <li>Compatible with colour-treated hair when using zinc pyrithione-based formulas.</li>
</ul>
<h3>&#x26A0;&#xFE0F; Considerations</h3>
<ul>
  <li>Some formulas can be drying to the hair shaft — always condition the lengths after use.</li>
  <li>Ketoconazole variants may cause temporary hair colour change in light-coloured or bleached hair.</li>
  <li>Should not replace your regular shampoo entirely — alternate with a gentle everyday shampoo.</li>
  <li>Overuse (more than 3x per week) can disrupt the scalp microbiome.</li>
</ul>

<h2>Recommended Products</h2>
<ul>
  <li><strong>1% Ketoconazole Shampoo</strong> — the gold standard antifungal for dandruff. Look for formulas with added moisturisers to offset dryness.</li>
  <li><strong>Zinc Pyrithione Scalp Shampoo</strong> — gentler option, ideal for maintenance use 1–2x per week. Safe for colour-treated hair.</li>
  <li><strong>Selenium Sulphide Shampoo (2.5%)</strong> — stronger option for stubborn dandruff; typically used as a short treatment course.</li>
  <li><strong>Piroctone Olamine Scalp Shampoo</strong> — a milder antifungal alternative suitable for sensitive scalps and daily use.</li>
</ul>

<h2>Pro Tips</h2>
<ul>
  <li>Apply to the scalp on <strong>dry or minimally wet hair</strong> for stubborn dandruff — this concentrates the actives and improves contact time.</li>
  <li>Use a <strong>scalp massage tool</strong> during application to improve product distribution and boost circulation.</li>
  <li>After rinsing, apply a <strong>light scalp toner with salicylic acid</strong> 2–3 times per week to break down any remaining buildup between washes.</li>
  <li>Avoid <strong>product buildup</strong> from dry shampoos and styling sprays — these feed Malassezia and worsen the cycle.</li>
  <li>Pair this with a <strong>gentle, sulfate-free everyday shampoo</strong> on non-treatment days to maintain scalp balance.</li>
</ul>

<h2>Warnings &amp; When to Stop</h2>
<p>Stop use immediately if you experience <strong>severe burning, blistering, or significant hair shedding</strong> beyond your normal rate. A mild tingling or initial increase in flaking during week 1 is normal as the treatment begins working.</p>
<p>If symptoms do not improve after <strong>6 weeks of consistent use</strong>, consult a dermatologist — your condition may require prescription-strength treatment or may have a different underlying cause (e.g. psoriasis, contact dermatitis).</p>
<p><strong>Pregnancy note:</strong> Consult your healthcare provider before using ketoconazole-based shampoos during pregnancy. Zinc pyrithione is generally considered safer during this phase.</p>
"""


def _mock_steps_for_scan(scan_type: str) -> list[dict]:
    """Return mock step data (including pre-built HTML descriptions) for MOCK_MODE."""
    if scan_type == "face":
        return [
            {
                "time_slot":    "morning",
                "title":        "Salicylic Cleanser",
                "product_name": "Salicylic Acid Cleanser",
                "bio":          "Gently removes excess oil and unclogs pores every morning.",
                "description":  _MOCK_HTML_FACE,
                "reason":       "Targets acne and oiliness detected in your face scan.",
            },
            {
                "time_slot":    "morning",
                "title":        "Niacinamide Pore Serum",
                "product_name": "Niacinamide Serum",
                "bio":          "Apply after cleansing to reduce pore size and balance sebum.",
                "description":  (
                    "<h2>About This Step</h2><p>Niacinamide (Vitamin B3) is a multi-functional "
                    "serum ingredient proven to regulate oil production, minimise pore appearance, "
                    "and even skin tone. Your scan identified enlarged pores and early pigmentation "
                    "— niacinamide addresses both simultaneously.</p>"
                    "<h2>How to Apply</h2><ol><li>Apply 3-4 drops to slightly damp skin after cleansing.</li>"
                    "<li>Press gently into skin — do not rub.</li>"
                    "<li>Wait 60 seconds before layering moisturiser on top.</li>"
                    "<li>Use every morning.</li></ol>"
                    "<h2>Pros &amp; Cons</h2><h3>&#x2705; Benefits</h3><ul>"
                    "<li>Regulates sebum production.</li>"
                    "<li>Visibly tightens pores over 4-6 weeks.</li>"
                    "<li>Reduces post-acne marks and uneven tone.</li>"
                    "<li>Extremely well-tolerated — suitable for sensitive skin.</li></ul>"
                    "<h3>&#x26A0;&#xFE0F; Considerations</h3><ul>"
                    "<li>Results take 4-6 weeks — patience required.</li>"
                    "<li>Do not mix with high-dose Vitamin C in the same step.</li>"
                    "<li>Choose a 5–10% concentration — higher is not always better.</li></ul>"
                    "<h2>Recommended Products</h2><ul>"
                    "<li><strong>5% Niacinamide + Zinc Serum</strong> — ideal for oily, acne-prone skin.</li>"
                    "<li><strong>10% Niacinamide Brightening Serum</strong> — for pigmentation focus.</li></ul>"
                    "<h2>Pro Tips</h2><ul>"
                    "<li>Layer it under SPF for added sebum control throughout the day.</li>"
                    "<li>Works synergistically with hyaluronic acid applied beforehand.</li></ul>"
                    "<h2>Warnings &amp; When to Stop</h2>"
                    "<p>If flushing or tingling occurs, switch to a lower-concentration formula (2–4%). "
                    "Discontinue if rash develops.</p>"
                ),
                "reason":       "Reduces pore size and balances excess sebum production.",
            },
            {
                "time_slot":    "night",
                "title":        "BHA Exfoliant Toner",
                "product_name": "BHA Exfoliant Toner",
                "bio":          "Apply after cleansing at night to clear pores and reduce dullness.",
                "description":  (
                    "<h2>About This Step</h2><p>A BHA (beta-hydroxy acid) toner delivers targeted "
                    "exfoliation after cleansing, dissolving dead cell buildup inside pores and "
                    "refining skin texture overnight. Unlike physical exfoliants, it is leave-on "
                    "and works gradually while you sleep.</p>"
                    "<h2>How to Apply</h2><ol>"
                    "<li>After your evening cleanse, apply with a cotton pad to the full face.</li>"
                    "<li>Avoid the eye area.</li>"
                    "<li>Do not rinse off — leave on overnight.</li>"
                    "<li>Use 3-4 nights per week initially, building to nightly if well tolerated.</li></ol>"
                    "<h2>Pros &amp; Cons</h2><h3>&#x2705; Benefits</h3><ul>"
                    "<li>Clears congested pores overnight.</li>"
                    "<li>Reduces blackheads and surface texture.</li>"
                    "<li>Improves product absorption for serums applied after.</li>"
                    "<li>Anti-inflammatory — calms redness around breakouts.</li></ul>"
                    "<h3>&#x26A0;&#xFE0F; Considerations</h3><ul>"
                    "<li>Do not use on the same night as retinol.</li>"
                    "<li>Increases sun sensitivity — critical to use SPF the following morning.</li>"
                    "<li>Start slowly (2-3 nights/week) to build tolerance.</li></ul>"
                    "<h2>Recommended Products</h2><ul>"
                    "<li><strong>2% BHA Leave-On Toner</strong> — most widely recommended for acne and oily skin.</li>"
                    "<li><strong>1% BHA Exfoliant Mist</strong> — gentler entry point for BHA beginners.</li></ul>"
                    "<h2>Pro Tips</h2><ul>"
                    "<li>Apply to slightly damp skin to reduce potential irritation.</li>"
                    "<li>Follow with a lightweight moisturiser — BHAs can feel drying initially.</li></ul>"
                    "<h2>Warnings &amp; When to Stop</h2>"
                    "<p>Stop if you develop intense peeling, raw skin, or burning that persists overnight. "
                    "Reduce frequency first before stopping entirely.</p>"
                ),
                "reason":       "Clears pores and reduces dullness overnight.",
            },
        ]
    elif scan_type == "hair_scalp":
        return [
            {
                "time_slot":    "weekly",
                "title":        "Antifungal Scalp Shampoo",
                "product_name": "Antifungal Scalp Shampoo",
                "bio":          "Use twice weekly to reduce dandruff and rebalance the scalp microbiome.",
                "description":  _MOCK_HTML_HAIR,
                "reason":       "Addresses dandruff and scalp oiliness detected in scan.",
            },
            {
                "time_slot":    "weekly",
                "title":        "Deep Conditioning Mask",
                "product_name": "Deep Conditioning Hair Mask",
                "bio":          "Apply weekly to restore moisture and reduce hair brittleness.",
                "description":  (
                    "<h2>About This Step</h2><p>A deep conditioning mask provides intensive hydration "
                    "and protein replenishment to the hair shaft, addressing the dryness and brittleness "
                    "that often accompanies a compromised scalp condition. Your scan identified hair "
                    "thinning and dullness — consistent masking helps restore the lipid layer of each strand.</p>"
                    "<h2>How to Apply</h2><ol>"
                    "<li>Apply to clean, towel-dried hair — avoid the scalp.</li>"
                    "<li>Comb through to ensure even distribution from mid-lengths to ends.</li>"
                    "<li>Leave on for 15-20 minutes under a shower cap to enhance penetration.</li>"
                    "<li>Rinse thoroughly with cool water to seal the cuticle.</li>"
                    "<li>Use once per week, or twice if hair is severely dry.</li></ol>"
                    "<h2>Pros &amp; Cons</h2><h3>&#x2705; Benefits</h3><ul>"
                    "<li>Dramatically improves hair softness and manageability within 1-2 uses.</li>"
                    "<li>Reduces breakage from daily styling and brushing.</li>"
                    "<li>Restores shine to dull, stressed hair.</li>"
                    "<li>Boosts elasticity — reduces snapping when hair is wet.</li></ul>"
                    "<h3>&#x26A0;&#xFE0F; Considerations</h3><ul>"
                    "<li>Avoid applying to the scalp — can worsen oiliness or clog follicles.</li>"
                    "<li>Protein-heavy masks should not be used more than twice per week on fine hair.</li>"
                    "<li>Requires adequate rinse time — product residue weighs hair down.</li></ul>"
                    "<h2>Recommended Products</h2><ul>"
                    "<li><strong>Keratin Repair Hair Mask</strong> — ideal for brittle, thinning hair.</li>"
                    "<li><strong>Argan Oil Deep Conditioner</strong> — adds shine and moisture without protein overload.</li></ul>"
                    "<h2>Pro Tips</h2><ul>"
                    "<li>Apply a few drops of lightweight hair oil over the mask for added penetration.</li>"
                    "<li>Use on wash days after your antifungal shampoo for a complete treatment.</li></ul>"
                    "<h2>Warnings &amp; When to Stop</h2>"
                    "<p>If hair feels limp or overly soft after use, you may be experiencing protein overload "
                    "— switch to a moisture-only formula for 4 weeks to rebalance.</p>"
                ),
                "reason":       "Restores moisture and reduces dryness and brittleness.",
            },
        ]
    else:  # product
        return [
            {
                "time_slot":    "morning",
                "title":        "Scanned Product Step",
                "product_name": "Scanned Product",
                "bio":          "Apply as directed in the morning after cleansing for best results.",
                "description":  (
                    "<h2>About This Step</h2><p>This step incorporates your recently scanned product "
                    "into your morning routine. Based on its formulation, it is best suited to the "
                    "morning slot where it can work synergistically with your SPF and provide active "
                    "benefits throughout the day.</p>"
                    "<h2>How to Apply</h2><ol>"
                    "<li>Cleanse skin thoroughly and pat dry.</li>"
                    "<li>Apply product as directed on the packaging.</li>"
                    "<li>Allow to absorb fully before layering additional products.</li>"
                    "<li>Finish with moisturiser and SPF 30+.</li></ol>"
                    "<h2>Pros &amp; Cons</h2><h3>&#x2705; Benefits</h3><ul>"
                    "<li>Directly addresses your identified skin concerns.</li>"
                    "<li>Formulated for daily use.</li>"
                    "<li>Complements your existing routine without duplication.</li></ul>"
                    "<h3>&#x26A0;&#xFE0F; Considerations</h3><ul>"
                    "<li>Always patch test new products for 48 hours before full-face use.</li>"
                    "<li>Check ingredient list against your known allergens before use.</li></ul>"
                    "<h2>Recommended Products</h2><ul>"
                    "<li>Look for formulas that match your scan-identified concerns and budget preference.</li></ul>"
                    "<h2>Pro Tips</h2><ul>"
                    "<li>Introduce one new product at a time so you can identify any reactions clearly.</li></ul>"
                    "<h2>Warnings &amp; When to Stop</h2>"
                    "<p>Discontinue and consult a dermatologist if irritation, redness, or breakouts "
                    "persist beyond 2 weeks of use.</p>"
                ),
                "reason":       "Incorporated per product instructions for best results.",
            },
        ]


# ── Shared generate-routine handler ──────────────────────────────────────────

async def _generate_routine(
    scan_id:   str,
    scan_type: str,   # "face" | "hair_scalp" | "product"
    user:      dict,
) -> RoutineGenerateResponse:
    """
    Core logic for all three generate-routine endpoints.

    TWO-PHASE APPROACH:
      Phase 1 — _call_claude_meta():
        Returns lightweight JSON with step metadata (no description).
        Reliable because JSON stays small (~200 bytes per step).

      Phase 2 — _generate_step_description() × N (concurrent):
        Returns raw HTML for each step. No JSON wrapping = no escape issues.
        All descriptions generated in parallel via asyncio.gather.
    """
    user_id = str(user["_id"])

    # ── Load scan doc ─────────────────────────────────────────────────────────
    try:
        oid = ObjectId(scan_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scan ID format.")

    db       = get_db()
    scan_doc = await db["scan_results"].find_one(
        {"_id": oid, "user_id": user_id, "scan_type": scan_type}
    )
    if not scan_doc:
        raise HTTPException(
            status_code=404,
            detail="Scan not found or does not belong to this user.",
        )

    # ── Load existing routine ─────────────────────────────────────────────────
    existing_steps = await _fetch_routine_steps(user_id)

    # ── Mock mode ─────────────────────────────────────────────────────────────
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — mock routine generation for user %s, scan %s", user_id, scan_id)
        mock_new_steps  = _mock_steps_for_scan(scan_type)
        saved_steps     = await _insert_steps(user_id, mock_new_steps)
        all_steps_after = await _fetch_routine_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = True,
            message         = "Added new steps based on your scan results. Keep it consistent!",
            added_steps     = saved_steps,
            existing_steps  = [_fmt_summary(s) for s in all_steps_after],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── Build Phase 1 prompt ──────────────────────────────────────────────────
    if scan_type == "product":
        prompt = _build_product_prompt(scan_doc, user, existing_steps)
    else:
        prompt = _build_scan_prompt(scan_doc, scan_type, user, existing_steps)

    # ── Phase 1: get step metadata (no descriptions) ──────────────────────────
    data = await _call_claude_meta(_ROUTINE_META_SYSTEM, prompt)

    # ── No update needed ──────────────────────────────────────────────────────
    if not data.get("needs_update", True):
        all_steps = await _fetch_routine_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = False,
            message         = str(data.get("message", "Your current routine is well-suited to your scan results.")),
            added_steps     = [],
            existing_steps  = [_fmt_summary(s) for s in all_steps],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── Parse + validate new steps (metadata only, description still empty) ───
    raw_new_steps = data.get("new_steps", [])
    if not isinstance(raw_new_steps, list):
        raise HTTPException(
            status_code=422,
            detail="AI returned 'new_steps' that is not a list.",
        )

    valid_steps: list[dict] = []
    for step in raw_new_steps[:5]:   # cap at 5
        if not isinstance(step, dict):
            continue
        product = str(step.get("product_name", "")).strip()
        if not product:
            continue
        valid_steps.append({
            "time_slot":    str(step.get("time_slot", "morning")).lower().strip(),
            "title":        str(step.get("title", product)).strip()[:80],
            "product_name": product[:120],
            "bio":          str(step.get("bio", "")).strip()[:300],
            "description":  "",   # filled in Phase 2
            "reason":       str(step.get("reason", "")).strip()[:200],
        })

    if not valid_steps:
        # Claude said needs_update=true but gave no valid steps
        all_steps = await _fetch_routine_steps(user_id)
        return RoutineGenerateResponse(
            routine_updated = False,
            message         = "Your current routine looks good for your scan results.",
            added_steps     = [],
            existing_steps  = [_fmt_summary(s) for s in all_steps],
            scan_id         = scan_id,
            scan_type       = scan_type,
        )

    # ── Phase 2: generate HTML descriptions concurrently ─────────────────────
    scan_context = _extract_scan_context(scan_doc, scan_type)

    html_results: list[str] = await asyncio.gather(
        *[_generate_step_description(step, user, scan_context) for step in valid_steps]
    )

    for step, html in zip(valid_steps, html_results):
        step["description"] = html

    # ── Save new steps to DB ──────────────────────────────────────────────────
    saved_steps = await _insert_steps(user_id, valid_steps)

    # ── Fetch full updated routine ────────────────────────────────────────────
    all_steps_after = await _fetch_routine_steps(user_id)

    return RoutineGenerateResponse(
        routine_updated = True,
        message         = str(data.get(
            "message",
            f"Added {len(saved_steps)} new step{'s' if len(saved_steps) != 1 else ''} "
            "to your routine based on your scan results."
        )),
        added_steps     = saved_steps,
        existing_steps  = [_fmt_summary(s) for s in all_steps_after],
        scan_id         = scan_id,
        scan_type       = scan_type,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

_COMMON_DESC_FOOTER = (
    "\n\n**Each generated step includes:**\n"
    "- `title` — 2-4 words linked to the product name\n"
    "- `bio` — 10-15 word one-line summary\n"
    "- `description` — full HTML (~2 pages): how to apply, pros & cons, "
    "recommended products, pro tips, and warnings\n\n"
    "**How it works:**\n"
    "1. Loads your scan results and current routine from the database.\n"
    "2. Phase 1: Claude decides which steps to add and returns lightweight metadata JSON.\n"
    "3. Phase 2: Claude generates a full HTML description for each step (run concurrently).\n"
    "4. New steps are **automatically saved** to your routine and returned in the response.\n\n"
    "**Response fields:**\n"
    "- `routine_updated: false` → current routine is already on track\n"
    "- `routine_updated: true` → new steps were added (check `added_steps`)\n"
    "- `existing_steps` → full current routine after update\n\n"
    f"**Active LLM backend:** {'🏠 LM Studio (local)' if USE_LOCAL_LLM else '☁️ Anthropic Claude'}"
)


@router.post(
    "/face/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate / Update Routine from Face Scan",
    description    = (
        "Analyses a face scan result and generates personalised skincare routine steps "
        "that address the detected conditions.\n\n"
        "Respects existing steps (no duplicates), user allergens, budget preference, "
        "and any special phases (pregnancy, menstrual cycle)."
        + _COMMON_DESC_FOOTER
    ),
    responses={
        400: {"description": "Invalid scan ID"},
        401: {"description": "Missing or invalid JWT"},
        404: {"description": "Scan not found"},
        502: {"description": "AI error"},
    },
)
async def generate_routine_from_face(
    scan_id:      str,
    current_user: CurrentUser,
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "face", current_user)


@router.post(
    "/hair_scalp/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate / Update Routine from Hair & Scalp Scan",
    description    = (
        "Analyses a hair/scalp scan result and generates personalised haircare routine "
        "steps that address the detected scalp and hair conditions.\n\n"
        "Respects existing steps, known allergens, budget preference, and "
        "phase-specific restrictions (e.g. avoids certain actives for pregnant users)."
        + _COMMON_DESC_FOOTER
    ),
    responses={
        400: {"description": "Invalid scan ID"},
        401: {"description": "Missing or invalid JWT"},
        404: {"description": "Scan not found"},
        502: {"description": "AI error"},
    },
)
async def generate_routine_from_hair_scalp(
    scan_id:      str,
    current_user: CurrentUser,
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "hair_scalp", current_user)


@router.post(
    "/product/{scan_id}/generate-routine",
    response_model = RoutineGenerateResponse,
    summary        = "Generate Routine Steps from Product Scan",
    description    = (
        "Takes a scanned product and generates routine steps showing **how to properly "
        "incorporate it** into the user's existing skincare or haircare routine.\n\n"
        "Considers: application timing, frequency, layering order relative to existing steps, "
        "user skin type, known allergens, and any phase-specific restrictions.\n\n"
        "If the user already has an equivalent product in their routine, Claude will "
        "flag this and suggest a swap instead of adding a duplicate."
        + _COMMON_DESC_FOOTER
    ),
    responses={
        400: {"description": "Invalid scan ID"},
        401: {"description": "Missing or invalid JWT"},
        404: {"description": "Scan not found"},
        502: {"description": "AI error"},
    },
)
async def generate_routine_from_product(
    scan_id:      str,
    current_user: CurrentUser,
) -> RoutineGenerateResponse:
    return await _generate_routine(scan_id, "product", current_user)