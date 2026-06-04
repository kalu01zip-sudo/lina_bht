"""
app/services/generate_manual_routine_ai.py
───────────────────────────────────────────
Async AI routine generator for manually entered products.
Uses AsyncAnthropic so the event loop is never blocked.
"""

import json
import logging
import os
import re

from anthropic import AsyncAnthropic

from app.utils.json_serializer import serialize_mongo

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else None


def _fmt_existing(existing: dict[str, list[str]]) -> str:
    parts = []
    for slot, cats in existing.items():
        if cats:
            parts.append(f"  {slot}: {', '.join(cats)}")
    return "\n".join(parts) if parts else "  (none)"


async def generate_manual_routine_ai(
    manual_product: dict,
    user_profile: dict,
    latest_face_scan: dict,
    existing_routines: list,
    existing_categories: dict[str, list[str]] | None = None,
) -> dict:
    existing_categories = existing_categories or {}
    existing_block = _fmt_existing(existing_categories)

    system_prompt = """
You are a skincare routine integration AI.

STRICT RULES:
- Return ONLY valid JSON
- No markdown
- No extra explanation
- Use the requested time exactly
- First step MUST be the user's manual product
- Avoid duplicate products already in saved routines
- Respect allergies, skin concerns, hair concerns, current phase, and budget
- If pregnant or postpartum, avoid retinoids and harsh exfoliation unless clearly safe
- Do not over-layer active ingredients

DUPLICATE RULES (critical):
- Do NOT recommend any category listed under ALREADY IN ROUTINE.
- Each step in the routine must have a unique category.
"""

    user_prompt = (
        "Create a routine around this manually entered product.\n\n"
        "MANUAL PRODUCT:\n"
        + json.dumps(serialize_mongo(manual_product), indent=2)
        + "\n\nUSER PROFILE:\n"
        + json.dumps(serialize_mongo(user_profile), indent=2)
        + "\n\nLATEST FACE SCAN:\n"
        + json.dumps(serialize_mongo(latest_face_scan), indent=2)
        + "\n\nCURRENT SAVED ROUTINES:\n"
        + json.dumps(serialize_mongo(existing_routines), indent=2)
        + f"\n\nALREADY IN ROUTINE (do NOT recommend these again):\n{existing_block}\n"
        + """
TASK:
Generate one complete routine for the requested time.
Use the manual product as step 1, then add only helpful complementary categories.
Generate 2 steps by default.
Use 3-4 steps only when the user's profile or scan clearly needs them.
Never return more than 4 steps.
Never return fewer than 2 steps.
Skip any category already listed in ALREADY IN ROUTINE.

Allowed time values:
morning, night, weekly

Allowed frequency values:
daily, alternate days, weekly

Allowed phase values:
repair, balance, maintenance

Allowed categories:
cleanser, toner, serum, moisturizer, sunscreen, mask, eye_care, exfoliant, oil, shampoo, conditioner

Return ONLY this JSON:

{
  "why": [
    "2-5 word point",
    "2-5 word point"
  ],
  "routine": {
    "time": "morning | night | weekly",
    "frequency": "daily | alternate days | weekly",
    "phase": "repair | balance | maintenance",
    "steps": [
      {
        "step": 1,
        "source": "manual",
        "category": "category",
        "product_name": "manual product name",
        "usage_reason": "10-15 word explanation"
      },
      {
        "step": 2,
        "source": "catalog",
        "category": "category",
        "usage_reason": "10-15 word explanation"
      }
    ]
  }
}
"""
    )

    response = await _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    )

    parsed = _extract_json(raw_text)
    if not parsed:
        logger.error("Manual routine AI returned unparseable JSON: %s", raw_text[:300])
        raise ValueError("Manual routine JSON failed: No JSON object returned")

    return parsed
