"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Claude Vision Face & Scalp Analyzer         ║
║                                                                  ║
║  Endpoints served:                                              ║
║   POST /face_scan           → skin health score + conditions    ║
║   POST /hair_and_scalp_scan → scalp health score + conditions   ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from claude_client import ClaudeClient


# ── Prompts ──────────────────────────────────────────────────────────────────

_FACE_SYSTEM = """\
You are a professional dermatologist AI embedded in a skincare app.
Analyze the face photo and return ONLY a valid JSON object — no markdown, no preamble.

JSON schema (strict):
{
  "score": <integer 0-100>,
  "score_recommendation_note": "<10-15 words summarising skin health and the single most important next step>",
  "detected_conditions": [
    {
      "condition": "<condition name, 1-3 words>",
      "detail": "<cause or recommendation, 7-10 words, one line>"
      "seriousness": "<mild|moderate|severe>"
    }
  ]
}

Scoring rubric (higher = healthier):
  90-100 : Clear, even-toned, well-hydrated skin with no visible concerns
  75-89  : Mostly healthy with minor issues (small pores, slight oiliness)
  60-74  : Visible concerns (mild acne, redness, early pigmentation)
  40-59  : Moderate issues (active breakouts, uneven tone, visible texture)
  0-39   : Significant concerns (severe acne, eczema, heavy pigmentation)

Rules:
- Return exactly 3 or 4 detected_conditions based on actual visible observations.
- Each detail: 7-10 words, single line, with a cause OR actionable recommendation.
- score_recommendation_note: 10-15 words, one sentence.
- Return ONLY the JSON, nothing else.
"""

_SCALP_SYSTEM = """\
You are a professional trichologist AI embedded in a haircare app.
Analyze the hair/scalp photo and return ONLY a valid JSON object — no markdown, no preamble.

JSON schema (strict):
{
  "score": <integer 0-100>,
  "score_recommendation_note": "<10-15 words summarising scalp/hair health and the single most important next step>",
  "detected_conditions": [
    {
      "condition": "<condition name, 1-3 words>",
      "detail": "<cause or recommendation, 7-10 words, one line>"
      "seriousness": "<mild|moderate|severe>"
    }
  ]
}

Scoring rubric (higher = healthier):
  90-100 : Healthy, well-moisturised scalp; strong, shiny hair
  75-89  : Mostly healthy with minor dryness or slight oiliness
  60-74  : Visible concerns (mild dandruff, early thinning, dull hair)
  40-59  : Moderate issues (moderate dandruff, oily scalp, visible shedding)
  0-39   : Significant concerns (severe dandruff, scalp inflammation, heavy hair loss)

Rules:
- Return exactly 3 or 4 detected_conditions based on actual visible observations.
- Each detail: 7-10 words, single line, with a cause OR actionable recommendation.
- score_recommendation_note: 10-15 words, one sentence.
- Return ONLY the JSON, nothing else.
"""

_FACE_USER  = "Analyze this face photo. Return the JSON skin health report."
_SCALP_USER = "Analyze this hair and scalp photo. Return the JSON scalp health report."


# ── Validator ────────────────────────────────────────────────────────────────

def _validate(data: dict) -> dict:
    score = max(0, min(100, int(data.get("score", 0))))

    note = str(data.get("score_recommendation_note", "")).strip()
    if not note:
        raise ValueError("Missing 'score_recommendation_note' in Claude response.")

    raw_conditions = data.get("detected_conditions", [])
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise ValueError("Missing or empty 'detected_conditions' in Claude response.")

    conditions = [
        {
            "condition": str(item.get("condition", "Unknown")).strip(),
            "detail":    str(item.get("detail", "")).strip(),
            "seriousness": str(item.get("seriousness", "unknown")).strip(),
        }
        for item in raw_conditions[:4]
        if isinstance(item, dict)
    ]

    if not conditions:
        raise ValueError("No valid conditions parsed from Claude response.")

    return {
        "score":                     score,
        "score_recommendation_note": note,
        "detected_conditions":       conditions,
    }


# ── Analyzer ─────────────────────────────────────────────────────────────────

class ClaudeVisionAnalyzer:
    """
    Face and scalp analyzer using Claude Vision.

    Usage:
        analyzer = ClaudeVisionAnalyzer(client)
        result   = analyzer.scan_face(image_bytes)
        result   = analyzer.scan_scalp(image_bytes)
    """

    def __init__(self, client: ClaudeClient):
        self._client = client

    def scan_face(self, image_bytes: bytes) -> dict:
        raw = self._client.vision_json(
            system      = _FACE_SYSTEM,
            user        = _FACE_USER,
            image_bytes = image_bytes,
        )
        return _validate(raw)

    def scan_scalp(self, image_bytes: bytes) -> dict:
        raw = self._client.vision_json(
            system      = _SCALP_SYSTEM,
            user        = _SCALP_USER,
            image_bytes = image_bytes,
        )
        return _validate(raw)
