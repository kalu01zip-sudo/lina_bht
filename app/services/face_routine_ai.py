"""
app/services/face_routine_ai.py
────────────────────────────────
Async AI routine generator for face scans.
Uses AsyncAnthropic so the event loop is never blocked.
"""

import json
import logging
import os
import re

from anthropic import AsyncAnthropic

from app.services.product_matcher import get_all_categories

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group()) if match else None


def _fmt_existing(existing: dict[str, list[str]]) -> str:
    """
    Format existing saved routine categories for the AI prompt.
    existing = { "morning": ["serum", "cleanser"], "night": ["face_moisturizer"], ... }
    """
    parts = []
    for slot, cats in existing.items():
        if cats:
            parts.append(f"  {slot}: {', '.join(cats)}")
    return "\n".join(parts) if parts else "  (none)"


async def generate_face_routine_ai(
    scan_data: dict,
    profile: dict,
    existing_categories: dict[str, list[str]] | None = None,
) -> dict:
    """
    Args:
        scan_data:           Scan analysis + nutritions from DB.
        profile:             User skin profile fields.
        existing_categories: Categories already saved per time-slot.
                             { "morning": ["serum", "cleanser"], ... }
                             AI is told to skip these entirely.
    """
    categories = get_all_categories()
    categories_str = ", ".join(categories)
    existing_categories = existing_categories or {}

    existing_block = _fmt_existing(existing_categories)

    system_prompt = f"""
You are a dermatology AI.

STRICT RULES:
- Return ONLY JSON
- Keep all text SHORT and UI-friendly
- "why" must be 4 bullet points, each 2-3 words only
- Do NOT include product names
- product_category MUST be one of:
{categories_str}

DUPLICATE RULES (critical):
- Within each time slot (morning / night / weekly), every product_category MUST be unique.
- Do NOT repeat the same product_category twice in the same slot under any circumstances.
- Do NOT recommend any category listed under ALREADY IN ROUTINE below.
"""

    user_prompt = f"""
User scan:
{json.dumps(scan_data)}

User profile:
{json.dumps(profile)}

ALREADY IN ROUTINE (do NOT recommend these again):
{existing_block}

Generate skincare routine.

Return JSON:

{{
  "why": ["Oil control", "Pigment care", "Hydration boost", "Barrier repair"],
  "morning": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "repair",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "sunscreen",
      "focus": "protection"
    }}
  ],
  "night": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "repair",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "moisturizer",
      "focus": "hydration"
    }}
  ],
  "weekly": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "maintenance",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "moisturizer",
      "focus": "hydration"
    }}
  ]
}}

IMPORTANT RULES:

- Always generate:
  - 4 morning steps (each with a DIFFERENT product_category)
  - 4 night steps (each with a DIFFERENT product_category)
  - 2 weekly care steps (each with a DIFFERENT product_category)

- Weekly care must NEVER be empty.

- Use ONLY categories that exist in product database:
  cleanser
  serum
  moisturizer
  sunscreen
  mask

- Do NOT generate:
  toner
  essence
  ampoule
  exfoliator
  cream cleanser
  gel cleanser
  foam cleanser

- Weekly care should usually include:
  - clay mask
  - hydrating mask
  - exfoliating mask

- CRITICAL: No two steps in the same slot may share the same product_category.
- CRITICAL: Skip any category already listed in ALREADY IN ROUTINE.
"""

    response = await _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=900,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = "".join(b.text for b in response.content if b.type == "text")
    result = _extract_json(raw)

    if not result:
        raise ValueError("AI returned invalid JSON for face routine")

    return result