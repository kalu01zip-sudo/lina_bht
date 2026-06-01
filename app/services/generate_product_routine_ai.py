from anthropic import Anthropic

import os
import json

from app.utils.json_serializer import (
    serialize_mongo
)


client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


# ==========================================
# SAFE JSON EXTRACTOR
# ==========================================

def extract_json(text: str):

    import re

    match = re.search(

        r'\{.*\}',

        text,

        re.DOTALL
    )

    if not match:

        return None

    candidate = match.group(0)

    parsed = json.loads(candidate)

    return parsed


# ==========================================
# GENERATE PRODUCT ROUTINE
# ==========================================

async def generate_product_routine_ai(

    product_scan: dict,

    latest_face_scan: dict,

    existing_routines: list
):

    system_prompt = """
You are a skincare routine integration AI.

STRICT RULES:
- Return ONLY valid JSON
- No markdown
- No extra explanation
- Avoid duplicate product usage
- Keep routine realistic
- Do not over-layer active ingredients
"""

    user_prompt = (
        "Integrate this scanned skincare product into user's routine.\n\n"

        "SCANNED PRODUCT:\n"
        + json.dumps(
            serialize_mongo(product_scan),
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

    5. Suggest complementary product categories ONLY

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

        print(
            "RAW PRODUCT ROUTINE RESPONSE:"
        )

        print(raw_text)

        raise Exception(
            f"Routine JSON failed: {str(e)}"
        )
