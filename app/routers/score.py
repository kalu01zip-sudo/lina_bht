# routers/score.py
"""
POST /score
Returns:
  {
    "Score":           int          [0-100]  weighted composite (face + hair + systemic)
    "face_score":      int          [0-100]  skin_type + skin_concern only
    "hair_score":      int | null   [0-100]  hair_type + hair_concern only (null if no hair data)
    "note":            str          (5-7 words referencing inputs & outputs)
    "hydration_score": int          [60-90]
    "acne_risk":       str          low | medium | high
    "sensitivity":     str          low | medium | high
  }

Scoring breakdown
─────────────────
face_score  → skin_type base  +  skin_concern penalties          (0-100)
hair_score  → hair_type base  +  hair_concern penalties          (0-100, null if absent)
Score       → face(50%) + hair(30%) + systemic(20%)              (0-100)
              or face(65%) + systemic(35%) when hair data absent

Systemic factors: age · current_phase · allergies
All numerics are deterministic Python.  AI writes the note only.
"""

import asyncio
from typing import Optional, List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.clients.claude_client import ClaudeClient

router = APIRouter(prefix="/score", tags=["Skin Health Score"])

# ── Type aliases ──────────────────────────────────────────────────────────────

PhaseType       = Literal["on_my_period", "pregnant", "postpartum", "menopause", "none"]
AllergyType     = Literal[
    "fragrance", "parabens", "formaldehyde", "phenoxyethanol",
    "retinol", "salicylic_acid", "benzoyl_peroxide", "alcohol_denat",
    "oxybenzone", "nickel", "sulfates", "alcohol",
]
SkinType        = Literal["dry", "combination", "normal", "sensitive", "oily"]
SkinConcernType = Literal["acne_pimple", "irritation_redness", "pigmentation", "dullness"]
HairType        = Literal["wavy", "straight", "curly", "coily_kinky"]
HairConcernType = Literal["hair_fall", "dandruff", "oily_scalp"]
RiskLevel       = Literal["low", "medium", "high"]

# ── Request / Response ────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    age:                  int                             = Field(..., ge=10, le=100)
    gender:               Literal["female"]               = "female"
    current_phase:        Optional[PhaseType]             = None
    allergies:            Optional[List[AllergyType]]     = None
    skin_type:            SkinType                        = Field(...)
    current_skin_concern: Optional[List[SkinConcernType]] = None
    hair_type:            Optional[HairType]              = None
    hair_concern:         Optional[List[HairConcernType]] = None


class ScoreResponse(BaseModel):
    Score:           int            = Field(..., ge=0,  le=100, description="Overall composite score")
    face_score:      int            = Field(..., ge=0,  le=100, description="Face & skin score")
    hair_score:      Optional[int]  = Field(None, ge=0, le=100, description="Hair & scalp score (null if no hair data)")
    note:            str            = Field(..., description="5-7 word note referencing inputs and outputs")
    hydration_score: int            = Field(..., ge=60, le=90)
    acne_risk:       RiskLevel
    sensitivity:     RiskLevel


# ═══════════════════════════════════════════════════════════════════════════════
#  FACE SCORE  [0-100]
#  Inputs: skin_type (base) + current_skin_concern (penalties)
# ═══════════════════════════════════════════════════════════════════════════════

_FACE_BASE: dict[str, float] = {
    "normal":      95.0,
    "combination": 80.0,
    "dry":         70.0,
    "sensitive":   65.0,
    "oily":        60.0,
}

_SKIN_CONCERN_PENALTY: dict[str, float] = {
    "acne_pimple":        25.0,
    "irritation_redness": 20.0,
    "pigmentation":       18.0,
    "dullness":           15.0,
}

_FACE_MAX_PENALTY = 65.0   # single concern can't wipe the score to zero


def compute_face_score(data: ScoreRequest) -> int:
    """
    Pure face score driven only by skin_type and skin_concern.
    Multiple concerns accumulate, capped so the floor never drops below ~15.
    """
    base     = _FACE_BASE.get(data.skin_type, 75.0)
    concerns = data.current_skin_concern or []
    penalty  = min(
        sum(_SKIN_CONCERN_PENALTY[c] for c in concerns),
        _FACE_MAX_PENALTY,
    )
    return max(0, min(100, round(base - penalty)))


# ═══════════════════════════════════════════════════════════════════════════════
#  HAIR SCORE  [0-100]  —  null when neither hair_type nor hair_concern present
#  Inputs: hair_type (base) + hair_concern (penalties)
# ═══════════════════════════════════════════════════════════════════════════════

_HAIR_BASE: dict[str, float] = {
    "straight":    95.0,
    "wavy":        88.0,
    "curly":       78.0,
    "coily_kinky": 68.0,
}

_HAIR_CONCERN_PENALTY: dict[str, float] = {
    "hair_fall":  30.0,
    "dandruff":   22.0,
    "oily_scalp": 18.0,
}

_HAIR_MAX_PENALTY = 55.0


def compute_hair_score(data: ScoreRequest) -> Optional[int]:
    """
    Pure hair/scalp score driven only by hair_type and hair_concern.
    Returns None when the user provided no hair information at all.
    """
    has_hair_type    = data.hair_type is not None
    has_hair_concern = bool(data.hair_concern)

    if not has_hair_type and not has_hair_concern:
        return None

    base    = _HAIR_BASE.get(data.hair_type or "", 85.0)   # fallback if only concerns given
    penalty = min(
        sum(_HAIR_CONCERN_PENALTY[h] for h in (data.hair_concern or [])),
        _HAIR_MAX_PENALTY,
    )
    return max(0, min(100, round(base - penalty)))


# ═══════════════════════════════════════════════════════════════════════════════
#  SYSTEMIC SCORE  (internal helper, 0-100)
#  Inputs: age · current_phase · allergies
# ═══════════════════════════════════════════════════════════════════════════════

def _age_factor(age: int) -> float:
    if   age < 20:  return 85.0
    elif age <= 25: return 100.0
    elif age <= 35: return 90.0
    elif age <= 45: return 78.0
    elif age <= 55: return 68.0
    else:           return 58.0


def _phase_factor(phase: Optional[str]) -> float:
    return {
        None:           100.0,
        "none":         100.0,
        "on_my_period":  78.0,
        "menopause":     70.0,
        "postpartum":    65.0,
        "pregnant":      62.0,
    }.get(phase, 100.0)


def _allergy_factor(allergies: Optional[List[str]]) -> float:
    if not allergies:
        return 100.0
    n       = len(allergies)
    penalty = 10.0 * (1.0 - 0.7 ** n) / 0.3
    return max(40.0, 100.0 - penalty)


def _systemic_score(data: ScoreRequest) -> float:
    """
    Internal 0-100 score for age, hormonal phase, and allergy burden.
    Weights: phase 45 % · age 30 % · allergies 25 %
    """
    return (
        _phase_factor(data.current_phase)  * 0.45
        + _age_factor(data.age)            * 0.30
        + _allergy_factor(data.allergies)  * 0.25
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  OVERALL SCORE  [0-100]
#  Composite of face_score · hair_score · systemic_score
# ═══════════════════════════════════════════════════════════════════════════════

def compute_overall_score(
    face_score: int,
    hair_score: Optional[int],
    data:       ScoreRequest,
) -> int:
    """
    When hair data is present:   face 50 % · hair 30 % · systemic 20 %
    When hair data is absent:    face 65 %              · systemic 35 %
    """
    systemic = _systemic_score(data)

    if hair_score is not None:
        raw = face_score * 0.50 + hair_score * 0.30 + systemic * 0.20
    else:
        raw = face_score * 0.65 + systemic * 0.35

    return max(0, min(100, round(raw)))


# ═══════════════════════════════════════════════════════════════════════════════
#  HYDRATION SCORE  [60-90]
# ═══════════════════════════════════════════════════════════════════════════════

def compute_hydration_score(data: ScoreRequest) -> int:
    raw = {"dry": 40.0, "sensitive": 55.0, "combination": 65.0,
           "normal": 78.0, "oily": 70.0}.get(data.skin_type, 60.0)

    concerns = data.current_skin_concern or []
    if "dullness"           in concerns: raw -= 12.0
    if "irritation_redness" in concerns: raw -= 6.0

    raw -= {None: 0, "none": 0, "on_my_period": 5,
            "menopause": 12, "postpartum": 8, "pregnant": 4}.get(data.current_phase, 0)

    if   data.age > 55: raw -= 15.0
    elif data.age > 45: raw -= 10.0
    elif data.age > 35: raw -= 5.0

    hydration_blocking = {"fragrance", "parabens", "phenoxyethanol", "alcohol_denat", "alcohol"}
    raw -= len(hydration_blocking & set(data.allergies or [])) * 2.5

    raw = max(0.0, min(100.0, raw))
    return round(60.0 + (raw / 100.0) * 30.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  ACNE RISK
# ═══════════════════════════════════════════════════════════════════════════════

def compute_acne_risk(data: ScoreRequest) -> RiskLevel:
    risk = float({"oily": 40, "combination": 20, "normal": 0,
                  "dry": -5, "sensitive": -5}.get(data.skin_type, 0))

    concerns = data.current_skin_concern or []
    if "acne_pimple"        in concerns: risk += 35.0
    if "irritation_redness" in concerns: risk += 8.0

    risk += {None: 0, "none": 0, "on_my_period": 15, "pregnant": 15,
             "postpartum": 10, "menopause": 5}.get(data.current_phase, 0)

    if data.age < 25: risk += 10.0
    if data.hair_concern and "oily_scalp" in data.hair_concern: risk += 5.0

    pore_clogging = {"parabens", "phenoxyethanol", "alcohol_denat", "oxybenzone", "sulfates"}
    risk += len(pore_clogging & set(data.allergies or [])) * 3.0

    return "high" if risk >= 55 else "medium" if risk >= 30 else "low"


# ═══════════════════════════════════════════════════════════════════════════════
#  SENSITIVITY
# ═══════════════════════════════════════════════════════════════════════════════

def compute_sensitivity(data: ScoreRequest) -> RiskLevel:
    sens = float({"sensitive": 45, "dry": 20, "combination": 5,
                  "normal": 0, "oily": -5}.get(data.skin_type, 0))

    concerns = data.current_skin_concern or []
    if "irritation_redness" in concerns: sens += 30.0

    sens += len(data.allergies or []) * 6.0
    reactive = {"retinol", "salicylic_acid", "benzoyl_peroxide", "formaldehyde", "fragrance"}
    sens += len(reactive & set(data.allergies or [])) * 10.0

    sens += {None: 0, "none": 0, "postpartum": 15, "pregnant": 12,
             "menopause": 10, "on_my_period": 8}.get(data.current_phase, 0)

    if data.age > 45: sens += 8.0

    return "high" if sens >= 65 else "medium" if sens >= 35 else "low"


# ═══════════════════════════════════════════════════════════════════════════════
#  AI NOTE  [5-7 words referencing the user's actual inputs + outputs]
# ═══════════════════════════════════════════════════════════════════════════════

def _build_note_context(
    data:        ScoreRequest,
    score:       int,
    face_score:  int,
    hair_score:  Optional[int],
    hydration:   int,
    acne_risk:   str,
    sensitivity: str,
) -> str:
    """Assemble a compact context string for the AI prompt."""
    skin_part = f"{data.skin_type} skin"
    concern_part = (
        ", ".join(data.current_skin_concern).replace("_", " ")
        if data.current_skin_concern else "no skin concerns"
    )
    hair_part = ""
    if data.hair_concern:
        hair_part = f", hair issues: {', '.join(data.hair_concern).replace('_', ' ')}"
    elif data.hair_type:
        hair_part = f", {data.hair_type} hair"

    phase_part = (
        f", phase: {data.current_phase.replace('_', ' ')}"
        if data.current_phase and data.current_phase != "none" else ""
    )

    hair_score_part = f", hair score {hair_score}/100" if hair_score is not None else ""

    return (
        f"Profile: {skin_part}, {concern_part}{hair_part}{phase_part}. "
        f"Scores — overall {score}/100, face {face_score}/100{hair_score_part}, "
        f"hydration {hydration}/90, acne risk {acne_risk}, sensitivity {sensitivity}."
    )


async def _generate_note(
    data:        ScoreRequest,
    score:       int,
    face_score:  int,
    hair_score:  Optional[int],
    hydration:   int,
    acne_risk:   str,
    sensitivity: str,
) -> str:
    """
    Generate a 5-7 word note via AI.
    The note must clearly reflect the user's actual inputs (skin type, concerns, phase)
    and outputs (scores, risk levels) — not generic advice.
    Falls back to a deterministic note on any error.
    """
    context = _build_note_context(
        data, score, face_score, hair_score, hydration, acne_risk, sensitivity
    )
    prompt = (
        f"{context}\n\n"
        "Write exactly ONE sentence of 5 to 7 words.\n"
        "The sentence must name a specific input (e.g. skin type, concern, phase) "
        "AND reference a specific output (e.g. acne risk, hydration, face score).\n"
        "Examples of the correct style:\n"
        "  'Oily skin raises your acne risk.'\n"
        "  'Acne and dullness lower face score.'\n"
        "  'Pregnancy phase impacts your sensitivity level.'\n"
        "Return ONLY the sentence. No quotes. End with a period."
    )

    try:
        client = ClaudeClient()
        note   = await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.text(system="", user=prompt, max_tokens=40)
        )
        note = note.strip().strip('"').strip("'")
        # Validate word count — fallback if AI ignores instruction
        if 5 <= len(note.split()) <= 8:
            return note
    except Exception:
        pass

    return _fallback_note(data, acne_risk, sensitivity, hydration, face_score)


def _fallback_note(
    data:        ScoreRequest,
    acne_risk:   str,
    sensitivity: str,
    hydration:   int,
    face_score:  int,
) -> str:
    """Deterministic 5-7 word note — never calls AI."""
    phase = (data.current_phase or "none").lower()
    skin  = data.skin_type.lower()
    concerns = data.current_skin_concern or []

    if phase == "pregnant":
        return "Pregnancy phase raises your sensitivity level."
    if phase == "postpartum":
        return "Postpartum phase lowers your skin score."
    if phase == "on_my_period":
        return "Period phase increases your acne risk."
    if "acne_pimple" in concerns and acne_risk == "high":
        return "Acne concern drives your high risk."
    if "irritation_redness" in concerns and sensitivity == "high":
        return "Redness concern raises your sensitivity level."
    if hydration <= 65:
        return f"{skin.capitalize()} skin lowers your hydration score."
    if face_score < 60:
        return f"{skin.capitalize()} skin reduces your face score."
    if acne_risk == "medium":
        return f"{skin.capitalize()} skin causes medium acne risk."
    return f"{skin.capitalize()} skin keeps sensitivity at {sensitivity}."


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTE
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("", response_model=ScoreResponse, summary="Skin Health Score")
async def calculate_score(payload: ScoreRequest):
    """
    Returns a full skin profile with three independent scores:

    | Field             | Type      | Range / Values             | Inputs used                     |
    |-------------------|-----------|----------------------------|---------------------------------|
    | `Score`           | int       | 0 – 100                    | face + hair + systemic (weighted)|
    | `face_score`      | int       | 0 – 100                    | skin_type, skin_concern         |
    | `hair_score`      | int/null  | 0 – 100  (null if absent)  | hair_type, hair_concern         |
    | `note`            | str       | 5-7 words                  | reflects inputs & outputs       |
    | `hydration_score` | int       | 60 – 90                    | skin_type, concern, phase, age  |
    | `acne_risk`       | str       | low / medium / high        | skin_type, concern, phase, age  |
    | `sensitivity`     | str       | low / medium / high        | skin_type, concern, allergies   |
    """
    face_score  = compute_face_score(payload)
    hair_score  = compute_hair_score(payload)
    score       = compute_overall_score(face_score, hair_score, payload)
    hydration   = compute_hydration_score(payload)
    acne_risk   = compute_acne_risk(payload)
    sensitivity = compute_sensitivity(payload)

    try:
        note = await _generate_note(
            payload, score, face_score, hair_score, hydration, acne_risk, sensitivity
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI note generation failed: {exc}") from exc

    return ScoreResponse(
        Score           = score,
        face_score      = face_score,
        hair_score      = hair_score,
        note            = note,
        hydration_score = hydration,
        acne_risk       = acne_risk,
        sensitivity     = sensitivity,
    )