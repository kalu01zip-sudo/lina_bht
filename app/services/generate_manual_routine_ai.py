from anthropic import Anthropic

import json
import os
import re

from app.utils.json_serializer import (
    serialize_mongo
)


client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


# ==========================================
# SAFE JSON EXTRACTOR
# ==========================================

def extract_json(
    text: str
):

    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL
    )

    if not match:

        return None

    return json.loads(
        match.group(0)
    )


# ==========================================
# GENERATE MANUAL PRODUCT ROUTINE
# ==========================================

async def generate_manual_routine_ai(

    manual_product: dict,

    user_profile: dict,

    latest_face_scan: dict,

    existing_routines: list
):

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
"""

    user_prompt = (
        "Create a routine around this manually entered product.\n\n"

        "MANUAL PRODUCT:\n"
        + json.dumps(
            serialize_mongo(manual_product),
            indent=2
        )

        + "\n\nUSER PROFILE:\n"
        + json.dumps(
            serialize_mongo(user_profile),
            indent=2
        )

        + "\n\nLATEST FACE SCAN:\n"
        + json.dumps(
            serialize_mongo(latest_face_scan),
            indent=2
        )

        + "\n\nCURRENT SAVED ROUTINES:\n"
        + json.dumps(
            serialize_mongo(existing_routines),
            indent=2
        )

        + """

TASK:
Generate one complete routine for the requested time.
Use the manual product as step 1, then add only helpful complementary categories.
Generate 2 steps by default.
Use 3-4 steps only when the user's profile or scan clearly needs them.
Never return more than 4 steps.
Never return fewer than 2 steps.

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

    response = client.messages.create(

        model="claude-haiku-4-5",

        max_tokens=1000,

        temperature=0,

        system=system_prompt,

        messages=[
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    )

    raw_text = ""

    for block in response.content:

        if hasattr(block, "text"):

            raw_text += block.text

    try:

        parsed = extract_json(raw_text)

        if not parsed:

            raise ValueError(
                "No JSON object returned"
            )

        return parsed

    except Exception as e:

        print("RAW MANUAL ROUTINE RESPONSE:")
        print(raw_text)

        raise Exception(
            f"Manual routine JSON failed: {str(e)}"
        )
