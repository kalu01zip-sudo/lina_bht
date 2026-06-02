"""
Face Skin AI Analysis Service
Uses Claude (claude-haiku-4-5) vision to analyse up to 5 face images.

Key design:
- A condition-specific penalty table drives all scores → no more generic or static numbers
- score_breakdown is returned so the client can show how the score was calculated
- _enforce_score_consistency() re-derives overall_score server-side as a safety net
- temperature=0.3 so different skin states produce genuinely different scores
"""

from anthropic import Anthropic
import base64
import os
import json
from app.utils.image_utils import optimise_image

# ── Client ────────────────────────────────────────────────────────────────────
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_json(text: str) -> str | None:
    """Return the first {...} block from a raw LLM response string."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return None


# ── Scoring constants ─────────────────────────────────────────────────────────

# Maps (condition_name, severity) → positive penalty deducted from baseline 95
FACE_PENALTY_TABLE: dict[str, dict[str, int]] = {
    "acne":               {"Mild": 8,  "Moderate": 18, "Severe": 32},
    "blackheads":         {"Mild": 4,  "Moderate": 10, "Severe": 18},
    "whiteheads":         {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "pores":              {"Mild": 3,  "Moderate": 8,  "Severe": 14},
    "oiliness":           {"Mild": 4,  "Moderate": 10, "Severe": 18},
    "dryness":            {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "dehydration":        {"Mild": 5,  "Moderate": 12, "Severe": 22},
    "redness":            {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "irritation":         {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "sensitivity":        {"Mild": 4,  "Moderate": 10, "Severe": 18},
    "pigmentation":       {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "dark_spots":         {"Mild": 4,  "Moderate": 10, "Severe": 18},
    "uneven_tone":        {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "dullness":           {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "dark_circles":       {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "eye_bags":           {"Mild": 4,  "Moderate": 9,  "Severe": 14},
    "fine_lines":         {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "wrinkles":           {"Mild": 7,  "Moderate": 16, "Severe": 28},
    "loss_of_elasticity": {"Mild": 6,  "Moderate": 14, "Severe": 24},
    "sun_damage":         {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "hyperpigmentation":  {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "melasma":            {"Mild": 6,  "Moderate": 14, "Severe": 24},
    "rosacea":            {"Mild": 7,  "Moderate": 16, "Severe": 28},
    "eczema":             {"Mild": 8,  "Moderate": 18, "Severe": 30},
    "psoriasis":          {"Mild": 10, "Moderate": 20, "Severe": 34},
    "contact_dermatitis": {"Mild": 7,  "Moderate": 16, "Severe": 26},
    "perioral_dermatitis":{"Mild": 6,  "Moderate": 14, "Severe": 24},
    "milia":              {"Mild": 3,  "Moderate": 7,  "Severe": 12},
    "syringoma":          {"Mild": 3,  "Moderate": 6,  "Severe": 10},
    "sebaceous_filaments": {"Mild": 3, "Moderate": 7,  "Severe": 12},
}

SCORE_BASELINE = 95
SCORE_MIN = 10

DEFAULT_ALLOWED_CONDITIONS: list[str] = sorted(FACE_PENALTY_TABLE.keys())

# Valid checked_area keys
CHECKED_AREA_KEYS = (
    "hydration", "sebum", "redness", "texture", "evenness", "pore_size",
    "acne", "blackheads", "whiteheads", "pigmentation", "hyperpigmentation",
    "dark_spots", "sun_spots", "freckles", "melasma", "dark_circles", "eye_bags",
    "fine_lines", "wrinkles", "crow_feet", "elasticity", "firmness", "sagging",
    "skin_tone", "tone_uniformity", "brightness", "dullness", "radiance",
    "sensitivity", "inflammation", "irritation", "barrier_health", "dryness",
    "oil_balance", "t_zone_oiliness", "cheek_dryness", "uv_damage", "sun_damage",
    "collagen_level", "skin_age", "oxidative_stress", "dehydration_risk",
    "acne_risk", "overall_skin_health", "skin_recovery_rate", "microbiome_balance",
)

VISIBLE_AREA_CONDITIONS = ("acne", "pimple", "redness", "irritation", "pigmentation", "dullness")
AREA_LOCATIONS = ("cheeks", "nose", "forehead", "chin", "under_eye")


def _build_penalty_table_text() -> str:
    """Build a markdown table of FACE_PENALTY_TABLE for prompt injection."""
    header = (
        "| Condition              | Mild | Moderate | Severe |\n"
        "|------------------------|------|----------|--------|\n"
    )
    rows = ""
    for cond, sev in sorted(FACE_PENALTY_TABLE.items()):
        rows += f"| {cond:<22} | {sev['Mild']:>4} | {sev['Moderate']:>8} | {sev['Severe']:>6} |\n"
    return header + rows


# ── Main analysis function ────────────────────────────────────────────────────

async def analyze_face_with_claude(
    images: list[bytes],
    allowed_conditions: list[str] | None = None,
) -> dict:
    """
    Analyse up to 5 face images with Claude vision.
    Returns a structured dict with overall_score, checked_area, detected_condition, etc.
    overall_score is always recalculated server-side from score_breakdown.
    """
    # ── Image optimisation ────────────────────────────────────────────────────
    optimised_images = []
    for img in images:
        opt_bytes, _ = optimise_image(img, "image/jpeg", max_px=720)
        optimised_images.append(opt_bytes)
    encoded_images = [base64.b64encode(img).decode("utf-8") for img in optimised_images]

    # ── Allowed conditions ────────────────────────────────────────────────────
    if not allowed_conditions:
        allowed_conditions = DEFAULT_ALLOWED_CONDITIONS
    conditions_str = ", ".join(allowed_conditions)

    penalty_table_text = _build_penalty_table_text()

    # ── System prompt ─────────────────────────────────────────────────────────
    system_prompt = f"""\
You are a highly critical, precise clinical dermatology AI system — an expert \
board-certified dermatologist specialising in skin health and aesthetics.

════════════════════════════════════════════════════
GOLDEN RULES
════════════════════════════════════════════════════
1. Return ONLY valid JSON — zero markdown, zero explanation.
2. Every score (0-100) must be derived from what you OBSERVE in the images.
   Perfect skin scores 90-95. Never output a static or "default" score.
3. Scores MUST differ between images with different conditions.
4. You have multiple face images for a 360° assessment — use all angles.

════════════════════════════════════════════════════
SCORING ALGORITHM  (follow exactly)
════════════════════════════════════════════════════
Baseline = {SCORE_BASELINE}
For each detected_condition subtract its penalty (positive integer) from the baseline.
overall_score = MAX({SCORE_MIN}, {SCORE_BASELINE} − penalty_1 − penalty_2 − penalty_3)

PENALTY TABLE (penalties are positive integers):
{penalty_table_text}
For any condition not in this table, estimate: Mild=5, Moderate=12, Severe=22.

SEVERITY → EXPECTED SCORE RANGE:
• All Mild       → overall_score 70-89
• Any Moderate   → overall_score 55-74
• Any Severe     → overall_score 10-54

════════════════════════════════════════════════════
CHECKED AREA SCORING
════════════════════════════════════════════════════
Health score 0-100 per area (100 = perfectly healthy):
• Active acne pustules visible      → acne       : Mild 35-55, Moderate 12-35, Severe 0-12
• Blackheads clearly visible        → blackheads : Mild 35-55, Moderate 12-35, Severe 0-12
• Oiliness / grease / shine         → sebum      : Mild 35-55, Moderate 12-35, Severe 0-12
• Dryness / flaking / tight skin    → dryness    : Mild 35-55, Moderate 12-35, Severe 0-12
• Redness / erythema visible        → redness    : Mild 35-55, Moderate 12-35, Severe 0-12
• Pigmentation / dark spots visible → pigmentation: Mild 40-60, Moderate 15-40, Severe 0-15
• Visible wrinkles                  → wrinkles   : Mild 40-65, Moderate 15-40, Severe 0-15
• No visible issue in an area       → that area scores 72-95
The 3 most problematic areas MUST score lower than the 2 healthiest areas.

════════════════════════════════════════════════════
HYDRATION
════════════════════════════════════════════════════
Estimate skin hydration (0-100) from visual cues:
• Visibly tight, flaky, ashy skin → 10-35
• Slightly dry but not flaking    → 36-55
• Normal, balanced moisture       → 56-74
• Plump, dewy, well-hydrated      → 75-95

hydration_target = recommended daily water intake in ml (1500-3500).

════════════════════════════════════════════════════
PROGNOSIS TIMELINE
════════════════════════════════════════════════════
Show the expected CHANGE in score (delta), not the final score.
Positive delta = improvement. Negative delta = worsening.

════════════════════════════════════════════════════
LIFESTYLE FACTORS
════════════════════════════════════════════════════
• stress_score:   100 = extreme stress signs visible (inflammation, breakouts)
• water_intake:   100 = excellent hydration visible
• sleep_quality:  100 = well-rested skin visible (no puffiness, no dark circles)
"""

    # ── User prompt ───────────────────────────────────────────────────────────
    schema_template = """\
Analyse all provided face images carefully using every angle. Observe: acne, \
blackheads, whiteheads, oiliness, dryness, redness, pigmentation, dark spots, \
fine lines, wrinkles, pore size, skin texture, tone evenness, hydration level, \
under-eye area, and overall skin radiance.

Follow these steps:
STEP 1 — OBSERVE what is visible across all images with clinical precision.
STEP 2 — CLASSIFY: pick exactly 3 conditions from the allowed list.
STEP 3 — SCORE: apply the PENALTY TABLE to compute overall_score.
STEP 4 — OUTPUT only this JSON (replace placeholder values):

{
  "overall_score": <integer computed from penalty table>,

  "checked_area": {
    "<area_name_1>": <health_score>,
    "<area_name_2>": <health_score>,
    "<area_name_3>": <health_score>,
    "<area_name_4>": <health_score>,
    "<area_name_5>": <health_score>
  },

  "visible_area": {
    "condition": "<one of: acne|pimple|redness|irritation|pigmentation|dullness>",
    "areas": ["<one or more of: cheeks|nose|forehead|chin|under_eye>"],
    "score": <health_score>
  },

  "hydration": <0-100>,

  "detected_condition": [
    {
      "name": "<condition_name_from_allowed_list>",
      "note": "<exactly 10-12 word clinical note>",
      "severity": "<Mild|Moderate|Severe>"
    },
    {
      "name": "<condition_name_from_allowed_list>",
      "note": "<exactly 10-12 word clinical note>",
      "severity": "<Mild|Moderate|Severe>"
    },
    {
      "name": "<condition_name_from_allowed_list>",
      "note": "<exactly 10-12 word clinical note>",
      "severity": "<Mild|Moderate|Severe>"
    }
  ],

  "lifestyle_factor": {
    "stress_score": <0-100>,
    "water_intake": <0-100>,
    "sleep_quality": <0-100>
  },

  "prognosis_timeline": {
    "seven_days": {
      "<checked_area_key>": <expected_score_delta>,
      "<checked_area_key>": <expected_score_delta>
    },
    "fourteen_days": {
      "<checked_area_key>": <expected_score_delta>,
      "<checked_area_key>": <expected_score_delta>
    }
  },

  "hydration_target": <recommended_daily_ml>,

  "score_breakdown": {
    "baseline": 95,
    "deductions": [
      {"condition": "<name>", "severity": "<Mild|Moderate|Severe>", "penalty": <positive_integer>},
      {"condition": "<name>", "severity": "<Mild|Moderate|Severe>", "penalty": <positive_integer>},
      {"condition": "<name>", "severity": "<Mild|Moderate|Severe>", "penalty": <positive_integer>}
    ],
    "final_score": <95 minus sum of penalties, minimum 10>
  }
}

HARD CONSTRAINTS:
1. checked_area keys MUST be from: """ + ", ".join(CHECKED_AREA_KEYS) + """
2. detected_condition names MUST be from: """ + "{conditions_str}" + """
3. Return exactly 5 checked_area items: the 3 lowest-scoring (worst) and 2 highest-scoring (best).
4. Return exactly 3 detected_condition items.
5. penalty values in score_breakdown are POSITIVE integers from the PENALTY TABLE.
6. final_score = 95 − sum(penalties), minimum 10.
7. overall_score MUST equal final_score.
8. All score values are integers 0-100. No string values. No explanation outside JSON.
"""

    user_prompt = schema_template.replace("{conditions_str}", conditions_str)

    # ── Build content blocks (all images + text) ──────────────────────────────
    content = []
    for img in encoded_images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img,
            },
        })
    content.append({"type": "text", "text": user_prompt})

    # ── API call ──────────────────────────────────────────────────────────────
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2500,
        temperature=0.5,   # slight variability → different skin states → different scores
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    # ── Parse & post-process ──────────────────────────────────────────────────
    try:
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        if not raw_text:
            raise ValueError("Empty Claude response")

        clean_json = extract_json(raw_text)
        if not clean_json:
            raise ValueError(f"No JSON object in Claude response: {raw_text[:300]}")

        result = json.loads(clean_json)
        result = _enforce_score_consistency(result)
        return result

    except Exception as exc:
        print(f"[ERROR] CLAUDE RAW RESPONSE: {response}")
        raise RuntimeError(f"Face analysis parsing failed: {exc}") from exc


# ── Score consistency enforcer ────────────────────────────────────────────────

def _enforce_score_consistency(data: dict) -> dict:
    """
    Server-side safety net.
    1. Recalculates overall_score from score_breakdown.deductions using FACE_PENALTY_TABLE.
    2. Falls back to severity-based caps when score_breakdown is absent.
    3. Clamps hydration to 0-100. Clamps hydration_target to 1500-3500 ml.
    """
    from app.core.mapping import normalize_condition

    detected = data.get("detected_condition") or []
    for c in detected:
        if isinstance(c, dict) and "name" in c:
            c["name"] = normalize_condition(c["name"])

    breakdown = data.get("score_breakdown") or {}
    deductions = breakdown.get("deductions") or []
    for d in deductions:
        if isinstance(d, dict) and "condition" in d:
            d["condition"] = normalize_condition(d["condition"])

    # ── Attempt 1: recalculate from score_breakdown ───────────────────────────
    recalculated = None
    if isinstance(deductions, list) and deductions:
        try:
            total_penalty = 0
            for d in deductions:
                if not isinstance(d, dict):
                    continue
                cond = d.get("condition", "")
                sev  = d.get("severity", "Moderate")
                if cond in FACE_PENALTY_TABLE and sev in FACE_PENALTY_TABLE[cond]:
                    total_penalty += FACE_PENALTY_TABLE[cond][sev]
                else:
                    raw_p = d.get("penalty", 0)
                    total_penalty += abs(int(raw_p))
            recalculated = max(SCORE_MIN, SCORE_BASELINE - total_penalty)
        except (TypeError, ValueError):
            recalculated = None

    # ── Attempt 2: severity-based cap ────────────────────────────────────────
    if recalculated is None:
        existing = data.get("overall_score", 72)
        has_severe   = any(c.get("severity") == "Severe"   for c in detected if isinstance(c, dict))
        has_moderate = any(c.get("severity") == "Moderate" for c in detected if isinstance(c, dict))

        if has_severe and existing > 54:
            recalculated = min(existing, 54)
        elif has_moderate and existing > 74:
            recalculated = min(existing, 74)
        else:
            recalculated = existing

    # ── Apply ─────────────────────────────────────────────────────────────────
    data["overall_score"] = recalculated

    if isinstance(breakdown, dict):
        breakdown["final_score"] = recalculated
        data["score_breakdown"] = breakdown

    # Clamp hydration
    hydration = data.get("hydration", 60)
    try:
        hydration = int(hydration)
    except (TypeError, ValueError):
        hydration = 60
    data["hydration"] = max(0, min(100, hydration))

    # Clamp hydration_target to a realistic ml range
    ht = data.get("hydration_target", 2000)
    try:
        ht = int(ht)
    except (TypeError, ValueError):
        ht = 2000
    data["hydration_target"] = max(1500, min(3500, ht))

    return data


# ── Identity verifier ─────────────────────────────────────────────────────────

async def verify_same_person(images: list[bytes]) -> dict:
    """
    Pre-flight check: confirm all 5 uploaded face images show the same person.
    Uses a lower-resolution optimised image to keep latency low.
    """
    optimised = []
    for img in images:
        opt_bytes, _ = optimise_image(img, "image/jpeg", max_px=512)
        optimised.append(opt_bytes)
    encoded = [base64.b64encode(img).decode("utf-8") for img in optimised]

    system_prompt = """\
You are an AI tasked with verifying if all provided images show the same person.
Return ONLY a JSON object — no markdown, no explanation.
"""
    user_prompt = "Do all these images show the exact same person? Return JSON: {\"same_person\": true or false, \"reason\": \"brief reason\"}"

    content = []
    for img in encoded:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img},
        })
    content.append({"type": "text", "text": user_prompt})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        raw_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
        clean_json = extract_json(raw_text)
        if clean_json:
            return json.loads(clean_json)
        return {"same_person": False, "reason": "Failed to parse AI verification response."}

    except Exception as exc:
        print(f"[ERROR] CLAUDE VERIFY ERROR: {exc}")
        return {"same_person": False, "reason": str(exc)}