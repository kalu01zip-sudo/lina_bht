"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Profile Scorer  v3                          ║
║                                                                  ║
║  Outputs (simplified, app-ready):                               ║
║   score        → int  50–95                                     ║
║   hydration    → int  50–78  (50=lowest, 78=highest)            ║
║   acne_risk    → "Low" | "Mild" | "High"                        ║
║   sensitivity  → "Low" | "Mild" | "High"                        ║
║   note         → str  8–10 word AI-personalized advice          ║
╚══════════════════════════════════════════════════════════════════╝

AI Note model: google/flan-t5-base (local, ~250MB, no GPU needed)
  Fallback:    rule-based note if model unavailable
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────
#  INPUT SCHEMA
# ─────────────────────────────────────────────────

@dataclass
class UserProfile:
    current_phase:  Optional[str] = None          # on_my_period | pregnant | postpartum
    allergies:      list[str]     = field(default_factory=list)
    skin_type:      Optional[str] = None           # dry | combination | normal | sensitive | oily
    skin_concerns:  list[str]     = field(default_factory=list)
    hair_type:      Optional[str] = None           # wavy | straight | curly | coily | kinky
    hair_concerns:  list[str]     = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
#  NORMALISATION HELPERS
#  Maps raw internal scores to the app-facing ranges.
#
#  Score:     raw 65–90  →  app 50–95
#  Hydration: raw 20–95  →  app 50–78
#
#  Formula (linear interpolation):
#    out = out_min + (raw - raw_min) / (raw_max - raw_min) * (out_max - out_min)
# ═══════════════════════════════════════════════════════════════

def _norm_score(raw: float,
                raw_min: float = 65, raw_max: float = 90,
                out_min: float = 50, out_max: float = 95) -> int:
    """Normalise raw care-intensity score → 50–95 int."""
    ratio = (raw - raw_min) / (raw_max - raw_min)
    ratio = max(0.0, min(1.0, ratio))           # clamp ratio to [0,1]
    return round(out_min + ratio * (out_max - out_min))


def _norm_hydration(raw: float,
                    raw_min: float = 20, raw_max: float = 95,
                    out_min: float = 50, out_max: float = 78) -> int:
    """Normalise raw hydration score → 50–78 int."""
    ratio = (raw - raw_min) / (raw_max - raw_min)
    ratio = max(0.0, min(1.0, ratio))
    return round(out_min + ratio * (out_max - out_min))


# ═══════════════════════════════════════════════════════════════
#  SECTION 1 — CARE INTENSITY SCORE  (raw 65–90)
# ═══════════════════════════════════════════════════════════════

_PHASE_WEIGHTS = {
    "on_my_period": 3,
    "pregnant":     7,
    "postpartum":   6,
}
_ALLERGEN_WEIGHTS = {
    "perfumes":       2,
    "essential_oils": 2,
    "sulfates":       2,
    "alcohol":        1,
}
_ALLERGEN_CUSTOM_WEIGHT = 1
_ALLERGEN_MAX           = 6

_SKIN_TYPE_WEIGHTS = {
    "sensitive":   6,
    "dry":         4,
    "oily":        3,
    "combination": 2,
    "normal":      0,
}
_SKIN_CONCERN_WEIGHTS = {
    "acne":         4,
    "pimple":       4,
    "irritation":   3,
    "redness":      3,
    "pigmentation": 2,
    "dullness":     1,
}
_SKIN_CONCERN_CUSTOM_WEIGHT = 2
_SKIN_CONCERN_MAX           = 8

_HAIR_TYPE_WEIGHTS = {
    "coily":    3,
    "kinky":    3,
    "curly":    2,
    "wavy":     1,
    "straight": 0,
}
_HAIR_CONCERN_WEIGHTS = {
    "hair_fall":  3,
    "dandruff":   2,
    "oily_scalp": 2,
    "dry_scalp":  2,
}
_HAIR_CONCERN_CUSTOM_WEIGHT = 1
_HAIR_CONCERN_MAX           = 5

_BASE_SCORE  = 65
_MAX_ADDABLE = 25


def _raw_care_score(profile: UserProfile) -> tuple[float, dict]:
    """Returns (raw_score 65–90, breakdown_dict)."""
    total  = 0
    detail = {}

    phase_key = (profile.current_phase or "").lower().replace(" ", "_")
    pts = _PHASE_WEIGHTS.get(phase_key, 0)
    detail["phase"] = pts
    total += pts

    ap = sum(_ALLERGEN_WEIGHTS.get(a.lower().replace(" ", "_"), _ALLERGEN_CUSTOM_WEIGHT)
             for a in profile.allergies)
    ap = min(ap, _ALLERGEN_MAX)
    detail["allergies"] = ap
    total += ap

    skin_key = (profile.skin_type or "").lower()
    pts = _SKIN_TYPE_WEIGHTS.get(skin_key, 0)
    detail["skin_type"] = pts
    total += pts

    cp = sum(_SKIN_CONCERN_WEIGHTS.get(
                 c.lower().replace(" ", "_").replace("/", "_"),
                 _SKIN_CONCERN_CUSTOM_WEIGHT)
             for c in profile.skin_concerns)
    cp = min(cp, _SKIN_CONCERN_MAX)
    detail["skin_concerns"] = cp
    total += cp

    hair_key = (profile.hair_type or "").lower().replace("/", "_")
    pts = _HAIR_TYPE_WEIGHTS.get(hair_key, 0)
    detail["hair_type"] = pts
    total += pts

    hcp = sum(_HAIR_CONCERN_WEIGHTS.get(h.lower().replace(" ", "_"),
                                        _HAIR_CONCERN_CUSTOM_WEIGHT)
              for h in profile.hair_concerns)
    hcp = min(hcp, _HAIR_CONCERN_MAX)
    detail["hair_concerns"] = hcp
    total += hcp

    raw = _BASE_SCORE + min(total, _MAX_ADDABLE)
    return raw, detail


# ═══════════════════════════════════════════════════════════════
#  SECTION 2 — HYDRATION  (raw 20–95  →  normalised 50–78)
#
#  Multiplier design:
#    raw = baseline(60) × skin_multiplier × phase_multiplier
#          + concern_delta + allergen_delta
#
#  Multipliers (applied multiplicatively to baseline):
#    Skin type:
#      oily        × 1.40  (sebum retains moisture strongly)
#      normal      × 1.25  (healthy balanced barrier)
#      combination × 0.90  (mixed — some dry zones)
#      sensitive   × 0.85  (compromised barrier leaks water)
#      dry         × 0.60  (severely low moisture retention)
#
#    Phase multiplier (hormonal effect on barrier):
#      pregnant    × 0.92
#      postpartum  × 0.88  (biggest hormonal drop post-birth)
#      on_period   × 0.95
#      none        × 1.00
#
#  Additive deltas (applied after multiplication):
#    dullness    -8   (key dehydration visible sign)
#    dry_scalp   -6
#    hair_fall   -3
#    acne/pimple +3   (oily/sebum link)
#    sulfates    -6   (strips lipid barrier)
#    alcohol     -4
# ═══════════════════════════════════════════════════════════════

_HYD_BASELINE = 60.0

_HYD_SKIN_MULT = {
    "oily":        1.40,
    "normal":      1.25,
    "combination": 0.90,
    "sensitive":   0.85,
    "dry":         0.60,
}

_HYD_PHASE_MULT = {
    "postpartum":   0.88,
    "pregnant":     0.92,
    "on_my_period": 0.95,
}

_HYD_CONCERN_DELTA = {
    "dullness":   -8,
    "dry_scalp":  -6,
    "hair_fall":  -3,
    "acne":       +3,
    "pimple":     +3,
    "irritation": -2,
    "redness":    -2,
}

_HYD_ALLERGEN_DELTA = {
    "sulfates": -6,
    "alcohol":  -4,
}


def _raw_hydration(profile: UserProfile) -> float:
    skin_key  = (profile.skin_type or "").lower()
    skin_mult = _HYD_SKIN_MULT.get(skin_key, 1.0)

    phase_key  = (profile.current_phase or "").lower().replace(" ", "_")
    phase_mult = _HYD_PHASE_MULT.get(phase_key, 1.0)

    raw = _HYD_BASELINE * skin_mult * phase_mult

    for c in profile.skin_concerns + profile.hair_concerns:
        raw += _HYD_CONCERN_DELTA.get(c.lower().replace(" ", "_"), 0)

    for a in profile.allergies:
        raw += _HYD_ALLERGEN_DELTA.get(a.lower().replace(" ", "_"), 0)

    return max(20.0, min(95.0, raw))


# ═══════════════════════════════════════════════════════════════
#  SECTION 3 — ACNE RISK  (Low | Mild | High)
# ═══════════════════════════════════════════════════════════════

_ACNE_SKIN = {"oily": +5, "combination": +3, "normal": -2, "dry": -2, "sensitive": -1}
_ACNE_CONCERN = {"acne": +5, "pimple": +5, "oily_scalp": +2, "dandruff": +1, "dry_scalp": -1}
_ACNE_PHASE = {"on_my_period": +3, "pregnant": +4, "postpartum": +4}
_ACNE_ALLERGEN = {"sulfates": +1}


def _acne_risk(profile: UserProfile) -> str:
    pts = 0
    pts += _ACNE_SKIN.get((profile.skin_type or "").lower(), 0)
    for c in profile.skin_concerns + profile.hair_concerns:
        pts += _ACNE_CONCERN.get(c.lower().replace(" ", "_"), 0)
    pts += _ACNE_PHASE.get((profile.current_phase or "").lower().replace(" ", "_"), 0)
    for a in profile.allergies:
        pts += _ACNE_ALLERGEN.get(a.lower().replace(" ", "_"), 0)
    if pts >= 8:  return "High"
    if pts >= 4:  return "Mild"
    return "Low"


# ═══════════════════════════════════════════════════════════════
#  SECTION 4 — SENSITIVITY  (Low | Mild | High)
# ═══════════════════════════════════════════════════════════════

_SENS_SKIN = {"sensitive": +6, "dry": +2, "combination": +1, "normal": -1, "oily": -2}
_SENS_CONCERN = {"redness": +4, "irritation": +4, "pigmentation": +1, "acne": +1, "pimple": +1, "dullness": +1}
_SENS_ALLERGEN = {"perfumes": +4, "essential_oils": +4, "alcohol": +3, "sulfates": +2}
_SENS_PHASE = {"pregnant": +4, "postpartum": +3, "on_my_period": +2}
_SENS_CUSTOM_KEYWORDS = ["eczema", "dermatitis", "react", "sensitiv", "itch"]


def _sensitivity(profile: UserProfile) -> str:
    pts = 0
    pts += _SENS_SKIN.get((profile.skin_type or "").lower(), 0)
    for c in profile.skin_concerns:
        key = c.lower().replace(" ", "_")
        d = _SENS_CONCERN.get(key, 0)
        if d == 0 and any(w in key for w in _SENS_CUSTOM_KEYWORDS):
            d = 3
        pts += d
    for a in profile.allergies:
        pts += _SENS_ALLERGEN.get(a.lower().replace(" ", "_"), 2)
    pts += _SENS_PHASE.get((profile.current_phase or "").lower().replace(" ", "_"), 0)
    if pts >= 10: return "High"
    if pts >= 5:  return "Mild"
    return "Low"


# ═══════════════════════════════════════════════════════════════
#  SECTION 5 — AI NOTE GENERATOR
#
#  Model: google/flan-t5-base (transformers text2text-generation)
#    - Small (~250MB), runs on CPU, no API key needed
#    - Loaded once in a background thread on first use
#    - Falls back to rule-based note if model unavailable
#
#  Why flan-t5-base?
#    - Instruction-tuned: follows "write a short advice" prompts
#    - Much lighter than GPT-2 / Mistral for this short-text task
#    - Deterministic enough for consistent 8–10 word outputs
# ═══════════════════════════════════════════════════════════════

class _NoteGenerator:
    MODEL_ID  = "google/flan-t5-base"
    _pipe     = None
    _lock     = threading.Lock()
    _attempted = False

    def _ensure_loaded(self):
        with self._lock:
            if self._attempted:
                return
            self._attempted = True
            try:
                from transformers import pipeline
                print("  [NoteGen] Loading flan-t5-base for note generation ...", flush=True)
                self._pipe = pipeline(
                    "text2text-generation",
                    model=self.MODEL_ID,
                    max_new_tokens=24,
                    do_sample=False,       # deterministic — same profile = same note
                )
                print("  [NoteGen] Ready ✓")
            except Exception as e:
                print(f"  [NoteGen] Could not load model: {e}. Using rule-based fallback.")

    def _build_prompt(self, profile: UserProfile, score: int,
                      hydration: int, acne_risk: str, sensitivity: str) -> str:
        """Build a structured prompt that reliably produces 8–10 word advice."""
        parts = []
        if profile.skin_type:
            parts.append(f"{profile.skin_type} skin")
        if profile.current_phase:
            parts.append(profile.current_phase.replace("_", " "))
        top_concerns = (profile.skin_concerns + profile.hair_concerns)[:2]
        parts.extend(top_concerns)

        profile_summary = ", ".join(parts) if parts else "no specific concerns"

        return (
            f"Write a single skincare tip of exactly 8 to 10 words for someone with "
            f"{profile_summary}. "
            f"Their care score is {score}/95, hydration is {hydration}%, "
            f"acne risk is {acne_risk}, sensitivity is {sensitivity}. "
            f"Start with an action verb. Be specific and encouraging."
        )

    def _rule_based_note(self, profile: UserProfile, score: int,
                         hydration: int, acne_risk: str, sensitivity: str) -> str:
        """Fallback: deterministic rule-based 8–10 word note."""
        skin  = (profile.skin_type or "normal").lower()
        phase = (profile.current_phase or "").lower().replace(" ", "_")

        # Phase-specific notes (highest priority)
        if phase == "pregnant":
            return "Use gentle, fragrance-free products safe for pregnancy now."
        if phase == "postpartum":
            return "Rebuild your skin barrier with rich, calming moisturisers daily."
        if phase == "on_my_period":
            return "Boost hydration and calm inflammation during your hormonal cycle."

        # Acne + sensitivity combos
        if acne_risk == "High" and sensitivity == "High":
            return "Choose non-comedogenic, fragrance-free products to calm skin."
        if acne_risk == "High":
            return "Use a gentle salicylic cleanser to reduce excess pore-clogging oil."

        # Hydration-led notes
        if hydration <= 54:
            return "Prioritise hyaluronic acid serum and barrier-repair moisturiser daily."
        if hydration >= 73:
            return "Your hydration is great — maintain with a lightweight daily moisturiser."

        # Skin type notes
        if skin == "dry":
            return "Layer a ceramide moisturiser to lock in essential skin moisture."
        if skin == "oily":
            return "Use a niacinamide serum to balance sebum and minimise pores."
        if skin == "sensitive":
            return "Stick to minimal, fragrance-free ingredients to soothe reactive skin."
        if skin == "combination":
            return "Balance T-zone oiliness with a gentle gel moisturiser every day."

        # Score-based fallback
        if score >= 80:
            return "Build a consistent targeted routine to address your multiple concerns."
        if score >= 65:
            return "A simple daily cleanse and moisturise routine will serve you well."
        return "Keep up your current routine — your skin profile looks balanced."

    def generate(self, profile: UserProfile, score: int,
                 hydration: int, acne_risk: str, sensitivity: str) -> str:
        self._ensure_loaded()

        # Try AI model
        if self._pipe is not None:
            try:
                prompt = self._build_prompt(profile, score, hydration, acne_risk, sensitivity)
                out    = self._pipe(prompt)[0]["generated_text"].strip()

                # Ensure 8–10 words — trim if longer, fallback if too short
                words = out.split()
                if 8 <= len(words) <= 12:
                    # Accept up to 12, trim to 10 if needed
                    return " ".join(words[:10]).rstrip(".,;:") + "."
                # Model gave unexpected length → use fallback
            except Exception:
                pass

        return self._rule_based_note(profile, score, hydration, acne_risk, sensitivity)


# Module-level singleton — loaded once, reused for all requests
_note_gen = _NoteGenerator()


# ═══════════════════════════════════════════════════════════════
#  MAIN SCORER
# ═══════════════════════════════════════════════════════════════

class ProfileScorer:
    """
    Scores a UserProfile and returns 5 app-ready fields:
      score       int   50–95
      hydration   int   50–78
      acne_risk   str   Low | Mild | High
      sensitivity str   Low | Mild | High
      note        str   8–10 word AI-personalised advice
    """

    def score(self, profile: UserProfile) -> dict:

        # ── Compute raw values ───────────────────────
        raw_score, _breakdown = _raw_care_score(profile)
        raw_hydration         = _raw_hydration(profile)
        acne                  = _acne_risk(profile)
        sens                  = _sensitivity(profile)

        # ── Normalise to app ranges ──────────────────
        app_score     = _norm_score(raw_score)        # 50–95
        app_hydration = _norm_hydration(raw_hydration) # 50–78

        # ── AI note ──────────────────────────────────
        note = _note_gen.generate(profile, app_score, app_hydration, acne, sens)

        return {
            "score":       app_score,       # int  50–95
            "hydration":   app_hydration,   # int  50–78
            "acne_risk":   acne,            # "Low" | "Mild" | "High"
            "sensitivity": sens,            # "Low" | "Mild" | "High"
            "note":        note,            # 8–10 word personalised advice string
        }