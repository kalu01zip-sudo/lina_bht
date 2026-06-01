from anthropic import Anthropic
import base64
import os
import json

from app.services.product_scan_context import (
    build_product_scan_context
)

from app.services.ingredient_conflict_engine import (
    analyze_ingredient_conflicts
)


client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


# ==========================================
# SAFE JSON EXTRACTOR
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


from app.utils.image_utils import optimise_image

# ==========================================
# ENCODE IMAGE
# ==========================================

def encode_image(image_bytes: bytes):
    # Optimise before encoding
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg")
    return base64.b64encode(opt_bytes).decode("utf-8")


# ==========================================
# MAIN AI FUNCTION
# ==========================================

async def analyze_product_scan(

    user_id: str,

    image_bytes: bytes
):

    # ======================================
    # BUILD CONTEXT
    # ======================================

    context = build_product_scan_context(
        user_id
    )

    ingredient_memory = context.get(
        "ingredient_memory",
        {}
    )

    encoded_image = encode_image(
        image_bytes
    )

    # ======================================
    # SYSTEM PROMPT
    # ======================================

    system_prompt = """
You are an advanced skincare ingredient analysis AI.

STRICT RULES:
- Return ONLY valid JSON
- No markdown
- No explanation outside JSON
- All scores must be integers (0-100)
- Intensity must be:
low, medium, or high
- Keep sentences concise
- Use skincare-scientific reasoning
"""

    # ======================================
    # USER PROMPT
    # ======================================

    user_prompt = f"""
Analyze this skincare product image.

You must:
1. Detect product details from image
2. Compare with latest face scan
3. Compare with previously used products
4. Detect ingredient conflicts
5. Detect irritation risk
6. Determine suitability

USER CONTEXT:

Latest face scan:
{json.dumps(context.get("latest_face_scan"))}

Previous products:
{json.dumps(context.get("previous_products"))}

Ingredient memory:
{json.dumps(context.get("ingredient_memory"))}

Return ONLY JSON in this exact format:

{{
  "product": {{

    "name": string,

    "brand": string,

    "category": string
  }},

  "detected_ingredients": [
    string
  ],

  "analysis": {{

    "overall_score": int,

    "score_profile": {{

      "compatibility": int,

      "safety": int,

      "redness": int,

      "effectiveness": int,

      "evenness": int
    }},

    "compatibility_analysis": {{

      "ingredient_conflict": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 words sentence"
      }},

      "allergy_risk": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 words sentence"
      }}
    }},

    "product_benefits": {{

      "high_compatibility": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 words sentence"
      }},

      "ingredient_synergy": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 words sentence"
      }}
    }},

    "what_to_stop": [

      "4-15 word sentence",

      "4-15 word sentence"
    ],

    "what_to_do": [

      "4-15 word sentence",

      "4-15 word sentence"
    ],

    "learn_more":
      "40-50 word skincare explanation"
  }}
}}
"""

    # ======================================
    # CLAUDE CONTENT
    # ======================================

    content = [

        {
            "type": "image",

            "source": {

                "type": "base64",

                "media_type": "image/jpeg",

                "data": encoded_image
            }
        },

        {
            "type": "text",

            "text": user_prompt
        }
    ]

    # ======================================
    # CALL CLAUDE
    # ======================================

    response = client.messages.create(

        model="claude-haiku-4-5",

        max_tokens=1200,

        temperature=0,

        system=system_prompt,

        messages=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    # ======================================
    # PARSE RESPONSE
    # ======================================

    try:

        raw_text = ""

        for block in response.content:

            if hasattr(block, "text"):

                raw_text += block.text

        clean_json = extract_json(
            raw_text
        )

        if not clean_json:

            raise Exception(
                "Invalid Claude JSON"
            )

        return json.loads(clean_json)

    except Exception as e:

        print(
            "[ERROR] PRODUCT AI ERROR:",
            response
        )

        raise Exception(
            f"Claude parsing failed: {str(e)}"
        )