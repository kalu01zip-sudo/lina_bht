from anthropic import Anthropic

import base64
import os
import json


client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


from app.utils.image_utils import optimise_image

# ==========================================
# ENCODE IMAGE
# ==========================================

def encode_image(image_bytes: bytes):
    # Optimise before encoding
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg")
    return base64.b64encode(opt_bytes).decode("utf-8")


# ==========================================
# COSMETIC PRODUCT VALIDATION
# ==========================================

async def validate_cosmetic_product(image_bytes: bytes) -> dict:
    """
    Quick validation to check if image is a cosmetic/skincare product.
    Returns early if NOT cosmetic to avoid wasting tokens on extraction.
    
    Returns:
        {
            "is_cosmetic": bool,
            "error_reason": str | None  # "not_cosmetic", "image_unclear", or None
        }
    """
    
    encoded = encode_image(image_bytes)
    
    # Minimal prompt for fast validation
    validation_prompt = """Is this a cosmetic, skincare, personal care, or beauty product?
Answer with ONLY "yes" or "no"."""
    
    try:
        response = client.messages.create(
            model="claude-opus-4-1",  # Faster/cheaper model for validation
            max_tokens=10,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
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
                            "text": validation_prompt
                        }
                    ]
                }
            ]
        )
        
        answer = ""
        for block in response.content:
            if hasattr(block, "text"):
                answer += block.text.strip().lower()
        
        is_cosmetic = "yes" in answer
        
        return {
            "is_cosmetic": is_cosmetic,
            "error_reason": None if is_cosmetic else "not_cosmetic"
        }
    except Exception as e:
        # If validation fails, log and assume it might be cosmetic
        # (don't block legitimate products due to validation errors)
        print(f"Validation error: {e}")
        return {
            "is_cosmetic": True,
            "error_reason": None
        }


# ==========================================
# JSON EXTRACT
# ==========================================

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