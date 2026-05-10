from anthropic import Anthropic

import base64
import os
import json


client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


# ==========================================
# JSON EXTRACT
# ==========================================

import json
import re


def extract_json(text: str):

    # ======================================
    # FIND JSON BLOCK
    # ======================================

    match = re.search(

        r'\{.*\}',

        text,

        re.DOTALL
    )

    if not match:

        return None

    candidate = match.group(0)

    # ======================================
    # VALIDATE JSON
    # ======================================

    try:

        parsed = json.loads(candidate)

        return json.dumps(parsed)

    except Exception:

        return None


# ==========================================
# ENCODE IMAGE
# ==========================================

def encode_image(image_bytes: bytes):

    return base64.b64encode(
        image_bytes
    ).decode("utf-8")


# ==========================================
# EXTRACT PRODUCT
# ==========================================

async def extract_product_data(

    image_bytes: bytes
):

    encoded = encode_image(
        image_bytes
    )

    system_prompt = """
You are a skincare product OCR extraction AI.

STRICT RULES:
- Return ONLY valid JSON
- No explanation
- No markdown
- Extract ingredients carefully
"""

    user_prompt = """
Analyze this skincare product image.

Extract:

{
  "product_name": string,

  "brand": string,

  "category": string,

  "ingredients": [
    string
  ]
}

IMPORTANT:
- ingredients should be normalized lowercase
- no duplicate ingredients
- infer category if needed
"""

    content = [

        {
            "type": "image",

            "source": {

                "type": "base64",

                "media_type": "image/jpeg",

                "data": encoded
            }
        },

        {
            "type": "text",

            "text": user_prompt
        }
    ]

    response = client.messages.create(

        model="claude-sonnet-4-6",

        max_tokens=500,

        temperature=0,

        system=system_prompt,

        messages=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    raw_text = ""

    for block in response.content:

        if hasattr(block, "text"):

            raw_text += block.text

    clean_json = extract_json(
        raw_text
    )

    if not clean_json:

        raise Exception(
            "Invalid extraction JSON"
        )

    return json.loads(clean_json)