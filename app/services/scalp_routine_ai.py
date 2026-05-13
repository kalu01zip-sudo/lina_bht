from anthropic import Anthropic
import os
import json
import re

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group() if match else None

async def generate_scalp_routine_ai(scan_data: dict, profile: dict):
    system_prompt = """
You are a professional trichologist AI.

STRICT RULES:
- Return ONLY JSON
- Keep all text SHORT and UI-friendly
- "why" must be 4 bullet points, each 2-3 words only
- Do NOT include product names
- product_category MUST be one of: shampoo, conditioner, scalp_serum, hair_mask, scalp_oil, hair_oil
"""

    user_prompt = f"""
User scalp scan:
{json.dumps(scan_data)}

User profile:
{json.dumps(profile)}

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
- Use ONLY allowed product categories.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = "".join([b.text for b in response.content if b.type == "text"])
    clean = extract_json(raw)
    if not clean: raise Exception("Invalid AI response")
    return json.loads(clean)
