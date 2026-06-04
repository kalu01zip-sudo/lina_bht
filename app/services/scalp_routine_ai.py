"""
app/services/scalp_routine_ai.py
──────────────────────────────────
Async AI routine generator for scalp/hair scans.
Uses AsyncAnthropic so the event loop is never blocked.
"""

import json
import logging
import os
import re

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group()) if match else None


def _fmt_existing(existing: dict[str, list[str]]) -> str:
    parts = []
    for slot, cats in existing.items():
        if cats:
            parts.append(f"  {slot}: {', '.join(cats)}")
    return "\n".join(parts) if parts else "  (none)"


async def generate_scalp_routine_ai(
    scan_data: dict,
    profile: dict,
    existing_categories: dict[str, list[str]] | None = None,
) -> dict:
    existing_categories = existing_categories or {}
    existing_block = _fmt_existing(existing_categories)

    system_prompt = """
You are a professional trichologist AI.

STRICT RULES:
- Return ONLY JSON
- Keep all text SHORT and UI-friendly
- "why" must be 4 bullet points, each 2-3 words only
- Do NOT include product names
- product_category MUST be one of: shampoo, conditioner, scalp_serum, hair_mask, scalp_oil, hair_oil

DUPLICATE RULES (critical):
- Within each time slot (morning / night / weekly), every product_category MUST be unique.
- Do NOT repeat the same product_category twice in the same slot.
- Do NOT recommend any category listed under ALREADY IN ROUTINE.
"""

    user_prompt = f"""
User scalp scan:
{json.dumps(scan_data)}

User profile:
{json.dumps(profile)}

ALREADY IN ROUTINE (do NOT recommend these again):
{existing_block}

Generate a scalp and hair care routine.

Return JSON:

{{
  "why": ["Reduce flaking", "Balance oil", "Strengthen roots", "Improve shine"],
  "morning": [
    {{
      "phase": "cleansing",
      "product_category": "shampoo",
      "focus": "dandruff"
    }},
    {{
      "phase": "nourishing",
      "product_category": "conditioner",
      "focus": "hydration"
    }}
  ],
  "night": [
    {{
      "phase": "treatment",
      "product_category": "scalp_serum",
      "focus": "thinning"
    }}
  ],
  "weekly": [
    {{
      "phase": "deep_care",
      "product_category": "hair_mask",
      "focus": "damage"
    }}
  ]
}}

IMPORTANT RULES:
- Always generate at least 2 morning steps, 1 night step, and 1 weekly step.
- Each step in the same slot must have a DIFFERENT product_category.
- Use ONLY allowed product categories.
- Skip any category already listed in ALREADY IN ROUTINE.
"""

    response = await _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = "".join(b.text for b in response.content if b.type == "text")
    result = _extract_json(raw)

    if not result:
        raise ValueError("AI returned invalid JSON for scalp routine")

    return result
