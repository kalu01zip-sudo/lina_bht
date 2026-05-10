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
# JSON EXTRACT
# ==========================================

import json
import re


def extract_json(text: str):

    # ======================================
    # TRY DIRECT JSON
    # ======================================

    try:

        return json.loads(text)

    except Exception:
        pass

    # ======================================
    # FIND FIRST JSON OBJECT
    # ======================================

    start = text.find("{")

    if start == -1:

        return None

    brace_count = 0

    end_index = None

    for i in range(start, len(text)):

        char = text[i]

        if char == "{":

            brace_count += 1

        elif char == "}":

            brace_count -= 1

            if brace_count == 0:

                end_index = i + 1

                break

    if end_index is None:

        return None

    candidate = text[start:end_index]

    # ======================================
    # PARSE JSON
    # ======================================

    try:

        return json.loads(candidate)

    except Exception as e:

        print("JSON PARSE ERROR:")
        print(e)

        print("RAW RESPONSE:")
        print(text)

        return None

# ==========================================
# GENERATE FINAL ANALYSIS
# ==========================================

async def generate_product_analysis(

    extracted_product: dict,

    face_scan: dict,

    ingredient_memory: dict,

    ingredient_conflicts: list,

    ingredient_intelligence: dict

):

    system_prompt = """
You are a skincare compatibility explanation AI.

STRICT RULES:
- Return ONLY valid JSON
- No markdown
- No extra explanation
- Keep responses concise
- Sound scientifically accurate
- Avoid fear-based wording
"""

    user_prompt = f"""
Generate a skincare compatibility analysis.

PRODUCT:
{json.dumps(extracted_product)}

LATEST FACE SCAN:
{json.dumps(serialize_mongo(face_scan))}

INGREDIENT MEMORY:
{json.dumps(serialize_mongo(ingredient_memory))}

CONFLICTS:
{json.dumps(serialize_mongo(ingredient_conflicts))}

INGREDIENT INTELLIGENCE:
{json.dumps(serialize_mongo(ingredient_intelligence))}

Return ONLY this JSON structure:

{{
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
          "8-10 word explanation"
      }},

      "allergy_risk": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 word explanation"
      }}
    }},

    "product_benefits": {{

      "high_compatibility": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 word explanation"
      }},

      "ingredient_synergy": {{

        "score": int,

        "intensity":
          "low | medium | high",

        "why":
          "8-10 word explanation"
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
      "40-50 word explanation"
  }}
}}
"""

    response = client.messages.create(

        model="claude-sonnet-4-6",

        max_tokens=1200,

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

    clean_json = extract_json(
        raw_text
    )

    if not clean_json:

        raise Exception(
            "Invalid analysis JSON"
        )

    return clean_json