"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Profile Scorer                              ║
║                                                                  ║
║  Outputs (app-ready):                                           ║
║   score        → int  50–95                                     ║
║   hydration    → int  50–78                                     ║
║   acne_risk    → "Low" | "Mild" | "High"                        ║
║   sensitivity  → "Low" | "Mild" | "High"                        ║
║   note         → str  8–10 word AI-personalised advice          ║
║                                                                  ║
║  All scoring is rule-based (deterministic, zero latency).       ║
║  Note generation uses Claude text API with rule-based fallback. ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from claude_client import ClaudeClient


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
#  NORMALISATION
#  raw score 65–90  →  app 50–95
#  raw hydration 20–95  →  app 50–78
# ═══════════════════════════════════════════════════════════════

def _norm(raw: float, raw_min: float, raw_max: float, out_min: float, out_max: float) -> int:
    ratio = max(0.0, min(1.0, (raw - raw_min) / (raw_max - raw_min)))
    return round(out_min + ratio * (out_max - out_min))


# ═══════════════════════════════════════════════════════════════
#  SECTION 1 — CARE INTENSITY SCORE  (raw 65–90)
# ═══════════════════════════════════════════════════════════════

_PHASE_W = {"on_my_period": 3, "pregnant": 7, "postpartum": 6}

_ALLERGEN_W = {"perfumes": 2, "essential_oils": 2, "sulfates": 2, "alcohol": 1}
_ALLERGEN_CUSTOM_W = 1
_ALLERGEN_CAP      = 6

_SKIN_TYPE_W    = {"sensitive": 6, "dry": 4, "oily": 3, "combination": 2, "normal": 0}
_SKIN_CONCERN_W = {"acne": 4, "pimple": 4, "irritation": 3, "redness": 3, "pigmentation": 2, "dullness": 1}
_SKIN_CONCERN_CUSTOM_W = 2
_SKIN_CONCERN_CAP      = 8

_HAIR_TYPE_W    = {"coily": 3, "kinky": 3, "curly": 2, "wavy": 1, "straight": 0}
_HAIR_CONCERN_W = {"hair_fall": 3, "dandruff": 2, "oily_scalp": 2, "dry_scalp": 2}
_HAIR_CONCERN_CUSTOM_W = 1
_HAIR_CONCERN_CAP      = 5

_BASE_SCORE  = 65
_MAX_ADDABLE = 25


def _raw_care_score(p: UserProfile) -> float:
    total = 0

    total += _PHASE_W.get(_norm_key(p.current_phase), 0)

    ap     = sum(_ALLERGEN_W.get(_norm_key(a), _ALLERGEN_CUSTOM_W) for a in p.allergies)
    total += min(ap, _ALLERGEN_CAP)

    total += _SKIN_TYPE_W.get((p.skin_type or "").lower(), 0)

    cp     = sum(_SKIN_CONCERN_W.get(_concern_key(c), _SKIN_CONCERN_CUSTOM_W) for c in p.skin_concerns)
    total += min(cp, _SKIN_CONCERN_CAP)

    total += _HAIR_TYPE_W.get((p.hair_type or "").lower().replace("/", "_"), 0)

    hcp    = sum(_HAIR_CONCERN_W.get(_concern_key(h), _HAIR_CONCERN_CUSTOM_W) for h in p.hair_concerns)
    total += min(hcp, _HAIR_CONCERN_CAP)

    return _BASE_SCORE + min(total, _MAX_ADDABLE)


def _norm_key(s: Optional[str]) -> str:
    return (s or "").lower().replace(" ", "_")

def _concern_key(s: str) -> str:
    return s.lower().replace(" ", "_").replace("/", "_")


# ═══════════════════════════════════════════════════════════════
#  SECTION 2 — HYDRATION  (raw 20–95  →  50–78)
# ═══════════════════════════════════════════════════════════════

_HYD_SKIN_MULT  = {"oily": 1.40, "normal": 1.25, "combination": 0.90, "sensitive": 0.85, "dry": 0.60}
_HYD_PHASE_MULT = {"pregnant": 0.92, "postpartum": 0.88, "on_my_period": 0.95}
_HYD_DELTAS     = {
    "dullness": -8, "dry_scalp": -6, "hair_fall": -3,
    "acne": +3,     "pimple": +3,
    "sulfates": -6, "alcohol": -4,
}


def _raw_hydration(p: UserProfile) -> float:
    base = 60.0
    base *= _HYD_SKIN_MULT.get((p.skin_type or "").lower(), 1.0)
    base *= _HYD_PHASE_MULT.get(_norm_key(p.current_phase), 1.0)

    all_items = [_concern_key(c) for c in p.skin_concerns + p.hair_concerns]
    all_items += [_norm_key(a) for a in p.allergies]
    for key in all_items:
        base += _HYD_DELTAS.get(key, 0)

    return max(20.0, min(95.0, base))


# ═══════════════════════════════════════════════════════════════
#  SECTION 3 — ACNE RISK  (Low | Mild | High)
# ═══════════════════════════════════════════════════════════════

_ACNE_SKIN    = {"oily": 5, "combination": 3, "normal": 0, "dry": -1, "sensitive": 2}
_ACNE_CONCERN = {"acne": 5, "pimple": 5, "oily_scalp": 2, "redness": 1, "irritation": 1}
_ACNE_PHASE   = {"on_my_period": 3, "pregnant": 4, "postpartum": 4}
_ACNE_ALLERGEN= {"sulfates": 1}


def _acne_risk(p: UserProfile) -> str:
    pts  = _ACNE_SKIN.get((p.skin_type or "").lower(), 0)
    pts += sum(_ACNE_CONCERN.get(_concern_key(c), 0) for c in p.skin_concerns + p.hair_concerns)
    pts += _ACNE_PHASE.get(_norm_key(p.current_phase), 0)
    pts += sum(_ACNE_ALLERGEN.get(_norm_key(a), 0) for a in p.allergies)
    return "High" if pts >= 8 else "Mild" if pts >= 4 else "Low"


# ═══════════════════════════════════════════════════════════════
#  SECTION 4 — SENSITIVITY  (Low | Mild | High)
# ═══════════════════════════════════════════════════════════════

_SENS_SKIN     = {"sensitive": 6, "dry": 2, "combination": 1, "normal": -1, "oily": -2}
_SENS_CONCERN  = {"redness": 4, "irritation": 4, "pigmentation": 1, "acne": 1, "pimple": 1, "dullness": 1}
_SENS_ALLERGEN = {"perfumes": 4, "essential_oils": 4, "alcohol": 3, "sulfates": 2}
_SENS_PHASE    = {"pregnant": 4, "postpartum": 3, "on_my_period": 2}
_SENS_KEYWORDS = ["eczema", "dermatitis", "react", "sensitiv", "itch"]


def _sensitivity(p: UserProfile) -> str:
    pts  = _SENS_SKIN.get((p.skin_type or "").lower(), 0)
    for c in p.skin_concerns:
        key = _concern_key(c)
        d   = _SENS_CONCERN.get(key, 0)
        if d == 0 and any(w in key for w in _SENS_KEYWORDS):
            d = 3
        pts += d
    pts += sum(_SENS_ALLERGEN.get(_norm_key(a), 2) for a in p.allergies)
    pts += _SENS_PHASE.get(_norm_key(p.current_phase), 0)
    return "High" if pts >= 10 else "Mild" if pts >= 5 else "Low"


# ═══════════════════════════════════════════════════════════════
#  SECTION 5 — PERSONALISED NOTE
#  Claude text API → fallback to deterministic rule-based note
# ═══════════════════════════════════════════════════════════════

_NOTE_SYSTEM = """\
You are a professional skincare advisor. Write exactly ONE skincare tip \
of 8 to 10 words. Start with an action verb. Be specific and encouraging. \
Return only the tip — no quotes, no punctuation at the end unless it's a period.\
"""


def _build_note_prompt(p: UserProfile, score: int, hydration: int,
                       acne_risk: str, sensitivity: str) -> str:
    parts = []
    if p.skin_type:
        parts.append(f"{p.skin_type} skin")
    if p.current_phase:
        parts.append(p.current_phase.replace("_", " "))
    parts.extend((p.skin_concerns + p.hair_concerns)[:2])

    profile_str = ", ".join(parts) if parts else "no specific concerns"
    return (
        f"Profile: {profile_str}. "
        f"Care score {score}/95, hydration {hydration}%, "
        f"acne risk {acne_risk}, sensitivity {sensitivity}. "
        f"Write an 8-to-10-word skincare tip."
    )


def _rule_note(p: UserProfile, score: int, hydration: int,
               acne_risk: str, sensitivity: str) -> str:
    """Deterministic fallback note — no external call needed."""
    phase = _norm_key(p.current_phase)
    skin  = (p.skin_type or "normal").lower()

    if phase == "pregnant":
        return "Use gentle, fragrance-free products safe for pregnancy now."
    if phase == "postpartum":
        return "Rebuild your skin barrier with rich, calming moisturisers daily."
    if phase == "on_my_period":
        return "Boost hydration and calm inflammation during your hormonal cycle."
    if acne_risk == "High" and sensitivity == "High":
        return "Choose non-comedogenic, fragrance-free products to calm skin."
    if acne_risk == "High":
        return "Use a gentle salicylic cleanser to reduce excess pore-clogging oil."
    if hydration <= 54:
        return "Prioritise hyaluronic acid serum and barrier-repair moisturiser daily."
    if hydration >= 73:
        return "Your hydration is great — maintain with a lightweight daily moisturiser."
    if skin == "dry":
        return "Layer a ceramide moisturiser to lock in essential skin moisture."
    if skin == "oily":
        return "Use a niacinamide serum to balance sebum and minimise pores."
    if skin == "sensitive":
        return "Stick to minimal, fragrance-free ingredients to soothe reactive skin."
    if skin == "combination":
        return "Balance T-zone oiliness with a gentle gel moisturiser every day."
    if score >= 80:
        return "Build a consistent targeted routine to address your multiple concerns."
    return "Keep up your current routine — your skin profile looks balanced."


def _generate_note(
    client: Optional["ClaudeClient"],
    p: UserProfile,
    score: int,
    hydration: int,
    acne_risk: str,
    sensitivity: str,
) -> str:
    """Try Claude first; fall back to rule-based on any error."""
    if client is not None:
        try:
            prompt = _build_note_prompt(p, score, hydration, acne_risk, sensitivity)
            note   = client.text(system=_NOTE_SYSTEM, user=prompt, max_tokens=64).strip()
            words  = note.split()
            if 7 <= len(words) <= 12:
                return " ".join(words[:10]).rstrip(".,;:") + "."
        except Exception:
            pass   # silently fall through to rule-based
    return _rule_note(p, score, hydration, acne_risk, sensitivity)


# ═══════════════════════════════════════════════════════════════
#  MAIN SCORER
# ═══════════════════════════════════════════════════════════════

class ProfileScorer:
    """
    Scores a UserProfile and returns 5 app-ready fields.

    Args:
        client: ClaudeClient instance (optional).
                If None, note generation falls back to rule-based.
    """

    def __init__(self, client: Optional["ClaudeClient"] = None):
        self._client = client

    def score(self, profile: UserProfile) -> dict:
        raw_score    = _raw_care_score(profile)
        raw_hydration = _raw_hydration(profile)
        acne         = _acne_risk(profile)
        sens         = _sensitivity(profile)

        app_score     = _norm(raw_score,     65, 90, 50, 95)
        app_hydration = _norm(raw_hydration, 20, 95, 50, 78)

        note = _generate_note(self._client, profile, app_score, app_hydration, acne, sens)

        return {
            "score":       app_score,
            "hydration":   app_hydration,
            "acne_risk":   acne,
            "sensitivity": sens,
            "note":        note,
        }
