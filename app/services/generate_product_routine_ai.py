"""
app/services/generate_product_routine_ai.py
─────────────────────────────────────────────
Async AI routine generator for product scans.
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


async def generate_product_routine_ai(
    product_scan: dict,
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
- Avoid duplicate product usage
- Keep routine realistic
- Do not over-layer active ingredients

DUPLICATE RULES (critical):
- Do NOT recommend any category already listed under ALREADY IN ROUTINE.
- Each step in the returned routine must have a unique category.
"""

    user_prompt = (
        "Integrate this scanned skincare product into user's routine.\n\n"
        "SCANNED PRODUCT:\n"
        + json.dumps(serialize_mongo(product_scan), indent=2)
        + "\n\nLATEST FACE SCAN:\n"
        + json.dumps(serialize_mongo(latest_face_scan), indent=2)
        + "\n\nCURRENT SAVED ROUTINES:\n"
        + json.dumps(serialize_mongo(existing_routines), indent=2)
        + f"\n\nALREADY IN ROUTINE (do NOT recommend these again):\n{existing_block}\n"
        + """
    TASKS:
    1. Decide if product belongs:
    - morning
    - night
    - weekly care

    2. Decide:
    - daily usage
    - weekly usage
    - avoid overuse

    3. Avoid ingredient conflicts

    4. Avoid duplicate product layering

    5. Suggest complementary product categories ONLY — skip any in ALREADY IN ROUTINE

    Return ONLY this JSON:

    {
    "why": [
        "2-5 word point",
        "2-5 word point"
    ],

    "routine": {

        "time":
        "morning | night | weekly",

        "frequency":
        "daily | alternate days | weekly",

        "phase":
        "repair | balance | maintenance",

        "steps": [

        {

            "step": 1,

            "category": string,

            "usage_reason":
            "10-15 word explanation"
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
        logger.error("Product routine AI returned unparseable JSON: %s", raw_text[:300])
        raise ValueError("No JSON object returned from product routine AI")

    return parsed
