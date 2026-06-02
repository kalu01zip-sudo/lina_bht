"""
Scalp & Hair AI Analysis Service
Uses Claude (claude-haiku-4-5) vision to analyse a scalp/hair image.

Key design:
- A condition-specific penalty table drives all scores → no more generic "72"
- score_breakdown is returned so the client can show the calculation
- _enforce_score_consistency() re-derives overall_score server-side as a safety net
- temperature=0.3 so identical images still get slightly varied emphasis areas
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
    """Return the first {...} block from a raw string."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return None


# ── Scoring constants (also injected into the prompt) ─────────────────────────

# Maps (condition_name, severity) → positive penalty deducted from baseline 95
PENALTY_TABLE: dict[str, dict[str, int]] = {
    "dandruff":                {"Mild": 8,  "Moderate": 18, "Severe": 30},
    "oily_scalp":              {"Mild": 6,  "Moderate": 14, "Severe": 24},
    "dry_scalp":               {"Mild": 7,  "Moderate": 15, "Severe": 26},
    "redness":                 {"Mild": 5,  "Moderate": 13, "Severe": 22},
    "inflammation":            {"Mild": 8,  "Moderate": 18, "Severe": 32},
    "sensitivity":             {"Mild": 4,  "Moderate": 10, "Severe": 18},
    "product_buildup":         {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "hair_thinning":           {"Mild": 10, "Moderate": 22, "Severe": 36},
    "hair_loss":               {"Mild": 12, "Moderate": 25, "Severe": 40},
    "split_ends":              {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "brittle_hair":            {"Mild": 6,  "Moderate": 14, "Severe": 22},
    "scalp_acne":              {"Mild": 7,  "Moderate": 16, "Severe": 28},
    "folliculitis":            {"Mild": 8,  "Moderate": 18, "Severe": 30},
    "seborrheic_dermatitis":   {"Mild": 10, "Moderate": 22, "Severe": 35},
    "psoriasis":               {"Mild": 12, "Moderate": 25, "Severe": 38},
    "eczema":                  {"Mild": 10, "Moderate": 20, "Severe": 34},
    "alopecia":                {"Mild": 14, "Moderate": 28, "Severe": 42},
    "telogen_effluvium":       {"Mild": 10, "Moderate": 22, "Severe": 35},
    "androgenetic_alopecia":   {"Mild": 12, "Moderate": 26, "Severe": 40},
    "scalp_fungus":            {"Mild": 8,  "Moderate": 18, "Severe": 30},
    "contact_dermatitis":      {"Mild": 7,  "Moderate": 16, "Severe": 26},
    "scalp_irritation":        {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "hair_breakage":           {"Mild": 6,  "Moderate": 14, "Severe": 22},
    "sebum_overproduction":    {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "hair_color_damage":       {"Mild": 4,  "Moderate": 10, "Severe": 16},
    "heat_damage":             {"Mild": 5,  "Moderate": 12, "Severe": 20},
    "chemical_damage":         {"Mild": 6,  "Moderate": 14, "Severe": 22},
}

SCORE_BASELINE = 95
SCORE_MIN = 10

# Default allowed conditions (overridden by the DB-fetched list at runtime)
DEFAULT_ALLOWED_CONDITIONS: list[str] = sorted(PENALTY_TABLE.keys())

# Valid checked_area keys accepted by the schema
CHECKED_AREA_KEYS = (
    "dandruff", "oiliness", "dryness", "redness", "inflammation", "sensitivity",
    "buildup", "folliculitis", "thinning", "hair_density", "hair_texture", "shine",
    "breakage", "split_ends", "scalp_elasticity", "follicles_health", "sebum_level",
    "hydration", "microbiome_balance", "fungal_activity",
)

# Valid visible_area conditions
VISIBLE_AREA_CONDITIONS = ("dandruff", "oiliness", "redness", "inflammation", "thinning", "buildup")

# Valid area locations
AREA_LOCATIONS = ("top", "crown", "sides", "hairline")


def _build_penalty_table_text() -> str:
    """Build a clean markdown table for the prompt from PENALTY_TABLE."""
    header = (
        "| Condition                  | Mild | Moderate | Severe |\n"
        "|----------------------------|------|----------|--------|\n"
    )
    rows = ""
    for cond, sev in sorted(PENALTY_TABLE.items()):
        rows += f"| {cond:<26} | {sev['Mild']:>4} | {sev['Moderate']:>8} | {sev['Severe']:>6} |\n"
    return header + rows


# ── Main analysis function ────────────────────────────────────────────────────

async def analyze_scalp_with_claude(
    image_bytes: bytes,
    allowed_conditions: list[str] | None = None,
) -> dict:
    """
    Analyse a scalp/hair image with Claude vision.
    Returns a structured dict with overall_score, checked_area, detected_condition, etc.
    overall_score is always recalculated server-side from score_breakdown for consistency.
    """
    # ── Image optimisation ────────────────────────────────────────────────────
    # 800 px gives Claude enough detail for texture / flake / density analysis.
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg", max_px=800)
    encoded_image = base64.b64encode(opt_bytes).decode("utf-8")

    # ── Allowed conditions ────────────────────────────────────────────────────
    if not allowed_conditions:
        allowed_conditions = DEFAULT_ALLOWED_CONDITIONS
    conditions_str = ", ".join(allowed_conditions)

    penalty_table_text = _build_penalty_table_text()

    # ── System prompt ─────────────────────────────────────────────────────────
    system_prompt = f"""\
You are a highly critical, precise clinical trichology AI system — an expert \
board-certified trichologist specialising in scalp and hair health.

════════════════════════════════════════════════════
GOLDEN RULES
════════════════════════════════════════════════════
1. Return ONLY valid JSON — zero markdown, zero explanation.
2. Every score (0-100) must be derived from what you OBSERVE in the image.
   A healthy scalp scores 90-95. Never output a static or "default" score.
3. Scores MUST differ between different images with different conditions.

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
• Clearly visible dandruff/flakes  → dandruff   : Mild 30-55, Moderate 12-30, Severe 0-12
• Visible oiliness/grease          → oiliness   : Mild 35-55, Moderate 12-35, Severe 0-12
• Visible dryness/tight scalp      → dryness    : Mild 30-55, Moderate 12-30, Severe 0-12
• Redness or irritation            → redness    : Mild 35-55, Moderate 12-35, Severe 0-12
• Thinning / density loss          → hair_density: Mild 35-60, Moderate 12-35, Severe 0-12
• No visible issue in an area      → that area scores 72-95
The 3 most problematic areas MUST score lower than the 2 healthiest areas.

════════════════════════════════════════════════════
PROGNOSIS TIMELINE
════════════════════════════════════════════════════
Show the expected CHANGE in score (delta), not the final score.
Positive delta = improvement. Negative delta = worsening.
Example: "dandruff": 6  means dandruff score expected to improve by 6 points.

════════════════════════════════════════════════════
LIFESTYLE FACTORS
════════════════════════════════════════════════════
Each score (0-100) estimates the INFLUENCE of that factor:
• stress_impact:  100 = stress is a major driver of the condition
• hygiene_score:  100 = excellent hygiene observed
• dietary_factor: 100 = diet is likely contributing positively
"""

    # ── User prompt with concrete JSON schema ─────────────────────────────────
    # Use a raw string template — Python placeholders are inserted via .format()
    # so we avoid f-string brace escaping issues with JSON curly braces.
    schema_template = """\
Analyse the scalp/hair image carefully. Observe: flakiness, oiliness, redness, \
thinning patterns, hair texture, scalp colour, follicle density, breakage, \
surface irregularities, and overall scalp condition.

Follow these steps:
STEP 1 — OBSERVE what is visible in the image with clinical precision.
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
    "condition": "<one of: dandruff|oiliness|redness|inflammation|thinning|buildup>",
    "areas": ["<one or more of: top|crown|sides|hairline>"],
    "score": <health_score>
  },

  "scalp_health": <integer, same as overall_score ± 3>,

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
    "stress_impact": <0-100>,
    "hygiene_score": <0-100>,
    "dietary_factor": <0-100>
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

    # ── API call ──────────────────────────────────────────────────────────────
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": encoded_image,
            },
        },
        {
            "type": "text",
            "text": user_prompt,
        },
    ]

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        temperature=0.5,   # slight variability so different images yield different scores
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    # ── Parse & post-process ──────────────────────────────────────────────────
    try:
        raw_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
        clean_json = extract_json(raw_text)
        if not clean_json:
            raise ValueError(f"No JSON object found in Claude response: {raw_text[:300]}")

        result = json.loads(clean_json)
        result = _enforce_score_consistency(result)
        return result

    except Exception as exc:
        raise RuntimeError(f"Scalp analysis parsing failed: {exc}") from exc


# ── Score consistency enforcer ────────────────────────────────────────────────

def _enforce_score_consistency(data: dict) -> dict:
    """
    Server-side safety net.
    1. Recalculates overall_score from score_breakdown.deductions using PENALTY_TABLE.
    2. Falls back to severity-based caps if score_breakdown is missing.
    3. Clamps scalp_health to overall_score ± 5.
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
                # Use the penalty from our table if available; otherwise trust the AI value
                if cond in PENALTY_TABLE and sev in PENALTY_TABLE[cond]:
                    total_penalty += PENALTY_TABLE[cond][sev]
                else:
                    raw_penalty = d.get("penalty", 0)
                    total_penalty += abs(int(raw_penalty))

            recalculated = max(SCORE_MIN, SCORE_BASELINE - total_penalty)
        except (TypeError, ValueError):
            recalculated = None

    # ── Attempt 2: severity-based cap (fallback when breakdown is absent) ─────
    if recalculated is None:
        existing = data.get("overall_score", 72)
        has_severe   = any(c.get("severity") == "Severe"   for c in detected if isinstance(c, dict))
        has_moderate = any(c.get("severity") == "Moderate" for c in detected if isinstance(c, dict))

        if has_severe and existing > 54:
            recalculated = min(existing, 54)
        elif has_moderate and existing > 74:
            recalculated = min(existing, 74)
        else:
            recalculated = existing  # trust the model

    # ── Apply ─────────────────────────────────────────────────────────────────
    data["overall_score"] = recalculated

    # Update score_breakdown.final_score to match
    if isinstance(breakdown, dict):
        breakdown["final_score"] = recalculated
        data["score_breakdown"] = breakdown

    # Clamp scalp_health within ±5 of overall_score
    sh = data.get("scalp_health", recalculated)
    try:
        sh = int(sh)
    except (TypeError, ValueError):
        sh = recalculated
    data["scalp_health"] = max(SCORE_MIN, min(100, sh))

    return data


# ── Image validator ───────────────────────────────────────────────────────────

async def validate_scalp_or_hair_image(image_bytes: bytes) -> dict:
    """
    Fast pre-flight check: confirm the uploaded image shows scalp or hair
    before running the full (and expensive) analysis pipeline.
    """
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg", max_px=512, quality=70)
    encoded_image = base64.b64encode(opt_bytes).decode("utf-8")

    system_prompt = """\
You are a strict image gatekeeper for a scalp and hair scan endpoint.
Return ONLY valid JSON — no markdown, no extra text.
"""

    user_prompt = """\
Does this image clearly show human scalp, hair, hairline, or close-up hair strands?

Return ONLY:
{
  "scalp_or_hair_detected": true or false,
  "reason": "one short sentence"
}

Return false for: product bottles, faces without visible scalp/hair focus,
full-body photos, objects, documents, animals, drawings, or blurry images.
"""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=100,
        temperature=0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encoded_image,
                    },
                },
                {"type": "text", "text": user_prompt},
            ],
        }],
    )

    try:
        raw_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
        clean_json = extract_json(raw_text)
        if not clean_json:
            raise ValueError("No JSON in validation response")
        parsed = json.loads(clean_json)
        return {
            "scalp_or_hair_detected": bool(parsed.get("scalp_or_hair_detected")),
            "reason": parsed.get("reason") or "Image does not clearly show scalp or hair.",
        }
    except Exception as exc:
        raise RuntimeError(f"Scalp validation parsing failed: {exc}") from exc
