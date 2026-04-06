# routers/score.py
"""
POST /score
Returns:
  {
    "Score":           int   [50-90],
    "note":            str   (7-10 words, Claude-generated),
    "hydration_score": int   [60-90],
    "acne_risk":       str   low | medium | high,
    "sensitivity":     str   low | medium | high
  }

All numeric values are calculated deterministically in Python.
Claude is used ONLY to write the one-line personalised note.
"""

import os
from typing import Optional, List, Literal

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
    Score:           int       = Field(..., ge=50, le=90,  description="Overall skin health score")
    note:            str       = Field(...,                description="7-10 word personalised tip")
    hydration_score: int       = Field(..., ge=60, le=90,  description="Skin hydration score")
    acne_risk:       RiskLevel = Field(...,                description="Acne risk level")
    sensitivity:     RiskLevel = Field(...,                description="Skin sensitivity level")


# ── Severity tables ───────────────────────────────────────────────────────────

_SKIN_CONCERN_SEVERITY: dict[str, int] = {
    "acne_pimple":        30,
    "irritation_redness": 25,
    "pigmentation":       22,
    "dullness":           18,
}

_HAIR_CONCERN_SEVERITY: dict[str, int] = {
    "hair_fall":  30,
    "dandruff":   22,
    "oily_scalp": 18,
}

# ── Sub-score helpers (each returns 0-100) ────────────────────────────────────

def _skin_concern_score(concerns: Optional[List[str]]) -> float:
    if not concerns:
        return 100.0
    total = sum(_SKIN_CONCERN_SEVERITY[c] for c in concerns)
    penalty = min(total * 0.85, 70.0)
    return max(30.0, 100.0 - penalty)

def _skin_type_score(skin_type: str) -> float:
    return {"normal": 100.0, "combination": 80.0, "dry": 68.0,
            "sensitive": 62.0, "oily": 58.0}.get(skin_type, 75.0)

def _phase_score(phase: Optional[str]) -> float:
    return {None: 100.0, "none": 100.0, "on_my_period": 75.0,
            "menopause": 68.0, "postpartum": 63.0, "pregnant": 60.0}.get(phase, 100.0)

def _allergy_score(allergies: Optional[List[str]]) -> float:
    if not allergies:
        return 100.0
    n = len(allergies)
    penalty = 10.0 * (1.0 - 0.7 ** n) / 0.3
    return max(40.0, 100.0 - penalty)

def _hair_score(hair_concerns: Optional[List[str]]) -> float:
    if not hair_concerns:
        return 100.0
    total = sum(_HAIR_CONCERN_SEVERITY[h] for h in hair_concerns)
    penalty = min(total * 0.80, 60.0)
    return max(40.0, 100.0 - penalty)

def _age_score(age: int) -> float:
    if   age < 20:  return 85.0
    elif age <= 25: return 100.0
    elif age <= 35: return 90.0
    elif age <= 45: return 78.0
    elif age <= 55: return 68.0
    else:           return 58.0

# ── Overall Skin Health Score [50-90] ─────────────────────────────────────────

def compute_score(data: ScoreRequest) -> tuple[int, dict]:
    sub_scores = {
        "skin_concern": _skin_concern_score(data.current_skin_concern),
        "skin_type":    _skin_type_score(data.skin_type),
        "phase":        _phase_score(data.current_phase),
        "allergy":      _allergy_score(data.allergies),
        "hair":         _hair_score(data.hair_concern),
        "age":          _age_score(data.age),
    }
    weights = {
        "skin_concern": 0.30, "skin_type": 0.20, "phase": 0.20,
        "allergy": 0.15,      "hair": 0.10,      "age":   0.05,
    }
    raw = sum(sub_scores[k] * weights[k] for k in weights)
    final = max(50, min(90, round(50.0 + (raw / 100.0) * 40.0)))
    return final, sub_scores

# ── Hydration Score [60-90] ───────────────────────────────────────────────────

def compute_hydration_score(data: ScoreRequest) -> int:
    """
    Step 1 — build a raw score (0-100) from skin signals.
    Step 2 — remap to [60, 90]:
                hydration = round(60 + (raw / 100) * 30)

    Baseline by skin type (raw):
        normal=78, oily=70, combination=65, sensitive=55, dry=40

    Deductions applied before remapping:
        dullness concern         → -12  (dehydrated skin = dull)
        irritation/redness       → -6   (broken barrier = moisture loss)
        menopause phase          → -12  (estrogen drop dries skin)
        postpartum phase         → -8
        pregnant phase           → -4
        on_my_period phase       → -5
        age > 55                 → -15
        age > 45                 → -10
        age > 35                 → -5
        each humectant-blocking  → -2.5 per allergen
        allergy (fragrance, parabens, phenoxyethanol, alcohol_denat, alcohol)

    Remap guarantees:
        Perfect raw (100) → 90  (max, flawless hydration)
        Worst  raw (0)    → 60  (min, still a liveable baseline)
    """
    # Step 1 — raw score
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

    raw = max(0.0, min(100.0, raw))   # clamp raw to [0, 100]

    # Step 2 — remap [0-100] → [60-90]
    return round(60.0 + (raw / 100.0) * 30.0)

# ── Acne Risk [low | medium | high] ──────────────────────────────────────────

def compute_acne_risk(data: ScoreRequest) -> RiskLevel:
    risk = 0.0
    risk += {"oily": 40, "combination": 20, "normal": 0,
             "dry": -5, "sensitive": -5}.get(data.skin_type, 0)

    concerns = data.current_skin_concern or []
    if "acne_pimple"        in concerns: risk += 35.0
    if "irritation_redness" in concerns: risk += 8.0

    risk += {None: 0, "none": 0, "on_my_period": 15, "pregnant": 15,
             "postpartum": 10, "menopause": 5}.get(data.current_phase, 0)

    if data.age < 25: risk += 10.0

    if data.hair_concern and "oily_scalp" in data.hair_concern:
        risk += 5.0

    pore_clogging = {"parabens", "phenoxyethanol", "alcohol_denat", "oxybenzone", "sulfates"}
    risk += len(pore_clogging & set(data.allergies or [])) * 3.0

    if   risk < 30:  return "low"
    elif risk <= 55: return "medium"
    else:            return "high"

# ── Sensitivity [low | medium | high] ────────────────────────────────────────

def compute_sensitivity(data: ScoreRequest) -> RiskLevel:
    sens = 0.0
    sens += {"sensitive": 45, "dry": 20, "combination": 5,
             "normal": 0, "oily": -5}.get(data.skin_type, 0)

    concerns = data.current_skin_concern or []
    if "irritation_redness" in concerns: sens += 30.0

    sens += len(data.allergies or []) * 6.0

    reactive = {"retinol", "salicylic_acid", "benzoyl_peroxide", "formaldehyde", "fragrance"}
    sens += len(reactive & set(data.allergies or [])) * 10.0

    sens += {None: 0, "none": 0, "postpartum": 15, "pregnant": 12,
             "menopause": 10, "on_my_period": 8}.get(data.current_phase, 0)

    if data.age > 45: sens += 8.0

    if   sens < 35:  return "low"
    elif sens <= 65: return "medium"
    else:            return "high"

# ── Claude note generator ─────────────────────────────────────────────────────

def _dominant_factor_label(data: ScoreRequest, sub_scores: dict) -> str:
    worst_key = min(sub_scores, key=sub_scores.get)
    return {
        "skin_concern": f"skin concerns ({', '.join(data.current_skin_concern or [])})",
        "skin_type":    f"{data.skin_type} skin type",
        "phase":        f"hormonal phase ({data.current_phase})",
        "allergy":      "multiple ingredient allergies",
        "hair":         f"hair/scalp issues ({', '.join(data.hair_concern or [])})",
        "age":          "age-related skin changes",
    }.get(worst_key, "overall skin health")


def _generate_note(
    data: ScoreRequest,
    score: int,
    sub_scores: dict,
    hydration: int,
    acne_risk: str,
    sensitivity: str,
) -> str:
    dominant = _dominant_factor_label(data, sub_scores)
    prompt = (
        f"You are a dermatologist. A {data.age}-year-old female scored:\n"
        f"  Skin health: {score}/90\n"
        f"  Hydration:   {hydration}/90\n"
        f"  Acne risk:   {acne_risk}\n"
        f"  Sensitivity: {sensitivity}\n"
        f"  Biggest concern: {dominant}\n\n"
        "Write exactly ONE sentence of 7 to 10 words.\n"
        "Give a specific, positive, actionable skincare tip targeting her biggest concern.\n"
        "Return ONLY the sentence. No quotes. End with a period."
    )
    client  = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=60,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip().strip('"').strip("'")

# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("", response_model=ScoreResponse, summary="Skin Health Score")
async def calculate_score(payload: ScoreRequest):
    """
    Returns a full skin profile:

    | Field             | Type | Range / Values         |
    |-------------------|------|------------------------|
    | `Score`           | int  | 50 – 90                |
    | `note`            | str  | 7-10 word tip (Claude) |
    | `hydration_score` | int  | 60 – 90                |
    | `acne_risk`       | str  | low / medium / high    |
    | `sensitivity`     | str  | low / medium / high    |
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")

    score,    sub_scores = compute_score(payload)
    hydration            = compute_hydration_score(payload)
    acne_risk            = compute_acne_risk(payload)
    sensitivity          = compute_sensitivity(payload)

    try:
        note = _generate_note(payload, score, sub_scores, hydration, acne_risk, sensitivity)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {exc}") from exc

    return ScoreResponse(
        Score           = score,
        note            = note,
        hydration_score = hydration,
        acne_risk       = acne_risk,
        sensitivity     = sensitivity,
    )