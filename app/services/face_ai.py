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


def _parse_regions(raw_regions: list) -> list[dict] | None:
    """Parse and validate normalised region coordinates from Claude's JSON response."""
    if not raw_regions or not isinstance(raw_regions, list):
        return None
    regions = []
    for r in raw_regions:
        if not isinstance(r, dict):
            continue
        try:
            x = float(r.get("x", 0.0))
            y = float(r.get("y", 0.0))
            w = float(r.get("width", 0.0))
            h = float(r.get("height", 0.0))
            # Validate ranges
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 <= w <= 1.0 and 0.0 <= h <= 1.0):
                continue
            if w < 0.01 or h < 0.01:
                continue
            regions.append({"x": x, "y": y, "width": w, "height": h})
        except (TypeError, ValueError):
            continue
    return regions if regions else None


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

async def detect_front_facing_image(images: list[bytes]) -> int:
    """
    Given a list of face images, returns the index (0-based) of the most 
    front-facing image, ideal for YouCam HD skin analysis.
    """
    encoded = [base64.b64encode(img).decode("utf-8") for img in images]

    system_prompt = """\
You are an AI tasked with selecting the most front-facing image from a provided set.
You will be given multiple images. You must return ONLY the 0-based integer index of the image that is the most perfectly front-facing, straight-on view of the face.
If multiple images are front-facing, pick the clearest one.
Return ONLY a JSON object: {"front_facing_index": <integer>} - no markdown, no explanation.
"""
    user_prompt = "Which of these images is the best front-facing selfie? Return the index (0-based)."

    content = []
    for i, img in enumerate(encoded):
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
            result = json.loads(clean_json)
            return result.get("front_facing_index", 0)
        return 0
    except Exception as exc:
        print(f"[ERROR] CLAUDE FRONT-FACING DETECTION ERROR: {exc}")
        return 0


async def generate_narrative_from_youcam(
    youcam_data: dict,
    allowed_conditions: list[str] | None = None
) -> dict:
    """
    Generates the final structured JSON (with clinical notes, prognosis, etc.)
    by feeding the YouCam scores to Claude (TEXT ONLY).
    """
    if not allowed_conditions:
        allowed_conditions = DEFAULT_ALLOWED_CONDITIONS
    conditions_str = ", ".join(allowed_conditions)

    penalty_table_text = _build_penalty_table_text()

    system_prompt = f"""\
You are a highly critical, precise clinical dermatology AI system — an expert \
board-certified dermatologist specialising in skin health and aesthetics.

════════════════════════════════════════════════════
GOLDEN RULES
════════════════════════════════════════════════════
1. Return ONLY valid JSON — zero markdown, zero explanation.
2. You are provided with objective skin scores from YouCam AI (where higher is better).
3. Use these provided scores to formulate the checked_area health scores and write clinical notes for the detected conditions.

════════════════════════════════════════════════════
SCORING ALGORITHM (Follow exactly)
════════════════════════════════════════════════════
Baseline = {SCORE_BASELINE}
For each detected_condition subtract its penalty (positive integer) from the baseline.
overall_score = MAX({SCORE_MIN}, {SCORE_BASELINE} − penalty_1 − penalty_2 − penalty_3)

PENALTY TABLE (penalties are positive integers):
{penalty_table_text}
For any condition not in this table, estimate: Mild=5, Moderate=12, Severe=22.
"""

    schema_template = """\
You are given the following raw skin analysis scores from YouCam:
{youcam_data}

Based on these scores, generate a comprehensive clinical narrative.
Translate the YouCam scores into our specific schema.

CRITICAL ANTI-BIAS RULES:
- Pick conditions that are clinically relevant based on the YouCam scores.
- 0-100 scales: 100 means perfect health.
- If a YouCam score for a condition is low, include it as a detected condition.

Follow these steps:
STEP 1 — REVIEW the YouCam scores. Check which conditions have valid `mask_urls`.
STEP 2 — CLASSIFY: pick 5 to 7 detected conditions based on the worst (lowest) YouCam scores.
CRITICAL RULE FOR STEP 2: You MUST ONLY select conditions that actually have a URL in their `mask_urls` array in the YouCam data. If `mask_urls` is empty, missing, or null, DO NOT select that condition, even if it is severe!
STEP 3 — SCORE: apply the PENALTY TABLE to compute overall_score.
STEP 4 — OUTPUT only this JSON (replace placeholder values):

{{
  "overall_score": <integer computed from penalty table>,

  "checked_area": {{
    "<area_name_1>": <health_score>,
    "<area_name_2>": <health_score>,
    "<area_name_3>": <health_score>,
    "<area_name_4>": <health_score>,
    "<area_name_5>": <health_score>
  }},

  "hydration": <0-100>,

  "detected_condition": [
    {{
      "name": "<condition_name_from_allowed_list>",
      "note": "<exactly 10-12 word clinical note describing the severity based on YouCam score>",
      "severity": "<Mild|Moderate|Severe>"
    }}
  ],

  "lifestyle_factor": {{
    "stress_score": <0-100>,
    "water_intake": <0-100>,
    "sleep_quality": <0-100>
  }},

  "prognosis_timeline": {{
    "seven_days": {{
      "<checked_area_key>": <expected_score_delta>,
      "<checked_area_key>": <expected_score_delta>
    }},
    "fourteen_days": {{
      "<checked_area_key>": <expected_score_delta>,
      "<checked_area_key>": <expected_score_delta>
    }}
  }},

  "hydration_target": <recommended_daily_ml>,

  "score_breakdown": {{
    "baseline": 95,
    "deductions": [
      {{"condition": "<name>", "severity": "<Mild|Moderate|Severe>", "penalty": <positive_integer>}}
    ],
    "final_score": <95 minus sum of penalties, minimum 10>
  }}
}}

HARD CONSTRAINTS:
1. checked_area keys MUST be from: """ + ", ".join(CHECKED_AREA_KEYS) + """
2. detected_condition names MUST be from: """ + "{conditions_str}" + """
3. Return 5 to 7 checked_area items. You MUST include every condition you selected for `detected_condition` in `checked_area` as well.
4. Return 5 to 7 detected_condition items (minimum 5, maximum 7). ONLY select conditions that have a valid mask URL in YouCam data.
5. penalty values in score_breakdown are POSITIVE integers from the PENALTY TABLE.
6. final_score = 95 − sum(penalties), minimum 10.
7. overall_score MUST equal final_score.
8. All score values are integers 0-100.
"""

    user_prompt = (
        schema_template
        .replace("{conditions_str}", conditions_str)
        .replace("{youcam_data}", json.dumps(youcam_data, indent=2))
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=3000,
            temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": [{"type": "text", "text": user_prompt}]}],
        )

        raw_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )

        clean_json = extract_json(raw_text)
        if not clean_json:
            raise ValueError(f"No JSON object in Claude response: {raw_text[:300]}")

        result = json.loads(clean_json)
        result = _enforce_score_consistency(result)

        # Parse and populate image_url for detected conditions (regions has been removed)
        for cond in result.get("detected_condition", []):
            if isinstance(cond, dict):
                cond["image_url"] = None

        # Add phase field to each detected_condition
        result = _assign_condition_phases(result)

        result["model_scores"] = {}
        return result

    except Exception as exc:
        print(f"[ERROR] CLAUDE NARRATIVE GENERATION ERROR: {exc}")
        raise RuntimeError(f"Face narrative parsing failed: {exc}") from exc



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


# ── Phase assignment ──────────────────────────────────────────────────────────

# Severity → phase fallback (when no score is available in checked_area)
_SEVERITY_TO_PHASE = {
    "Severe":   "Needs care",
    "Moderate": "Average",
    "Mild":     "Good",
}


def _score_to_phase(score: int) -> str:
    """Map a 0-100 health score to a human-readable phase label."""
    if score <= 30:
        return "Needs care"
    if score <= 50:
        return "Average"
    if score <= 70:
        return "Good"
    return "Very Well"


# Mappings to reconcile detected_condition name and checked_area keys
_CONDITION_TO_CHECKED_AREA_KEYS = {
    "acne": ["acne"],
    "blackheads": ["blackheads"],
    "whiteheads": ["whiteheads"],
    "sebum": ["sebum"],
    "oiliness": ["sebum"],
    "dryness": ["dryness"],
    "dehydration": ["dryness", "hydration"],
    "redness": ["redness"],
    "irritation": ["redness"],
    "inflammation": ["redness"],
    "pigmentation": ["pigmentation"],
    "hyperpigmentation": ["pigmentation"],
    "dark_spots": ["pigmentation"],
    "sun_spots": ["pigmentation"],
    "freckles": ["pigmentation"],
    "melasma": ["pigmentation"],
    "wrinkles": ["wrinkles"],
    "fine_lines": ["wrinkles"],
    "crow_feet": ["wrinkles"],
    "pores": ["pore_size", "pores"],
    "pore_size": ["pore_size", "pores"],
    "enlarged_pores": ["pore_size", "pores"],
    "dark_circles": ["dark_circles"],
    "skin_tone": ["skin_tone"],
    "radiance": ["radiance"],
    "dullness": ["radiance", "skin_tone"],
    "evenness": ["evenness"],
    "texture": ["texture"],
    "elasticity": ["elasticity"],
    "firmness": ["firmness"],
    "sagging": ["sagging"],
}


def _assign_condition_phases(data: dict) -> dict:
    """
    Add a `phase` field to every item in `detected_condition`.

    Strategy (no AI call — pure logic):
      1. Look up the condition name in `checked_area`. If a matching score
         exists, convert the 0-100 health score to a phase label.
      2. Otherwise fall back to the severity field:
         Severe → "Needs care", Moderate → "Average", Mild → "Good".
    """
    checked_area = data.get("checked_area", {})

    for cond in data.get("detected_condition", []):
        if not isinstance(cond, dict):
            continue

        cond_name = cond.get("name", "").strip().lower().replace(" ", "_")
        severity = cond.get("severity", "Moderate")

        # Try to find a matching score in checked_area
        score = None
        # Try direct lookup
        if cond_name in checked_area:
            score = checked_area[cond_name]
        else:
            # Try mapping lookup
            mapped_keys = _CONDITION_TO_CHECKED_AREA_KEYS.get(cond_name, [])
            for k in mapped_keys:
                if k in checked_area:
                    score = checked_area[k]
                    break

        if score is not None:
            try:
                cond["phase"] = _score_to_phase(int(score))
            except (TypeError, ValueError):
                cond["phase"] = _SEVERITY_TO_PHASE.get(severity, "Average")
        else:
            cond["phase"] = _SEVERITY_TO_PHASE.get(severity, "Average")

    return data

# ── Identity verifier ─────────────────────────────────────────────────────────

async def verify_same_person(images: list[bytes]) -> dict:
    """
    Pre-flight check: confirm all 5 uploaded face images show the same person.
    Uses a lower-resolution optimised image to keep latency low.
    """
    # Images are already optimised in scan.py before calling this service.
    encoded = [base64.b64encode(img).decode("utf-8") for img in images]

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