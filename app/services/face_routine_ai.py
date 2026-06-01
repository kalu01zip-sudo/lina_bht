from anthropic import Anthropic
import os
import json
import re

from app.services.product_matcher import get_all_categories

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group() if match else None


async def generate_face_routine_ai(scan_data: dict, profile: dict):
    categories = get_all_categories()
    categories_str = ", ".join(categories)

    system_prompt = f"""
You are a dermatology AI.

STRICT RULES:
- Return ONLY JSON
- Keep all text SHORT and UI-friendly
- "why" must be 4 bullet points, each 2-3 words only
- Do NOT include product names
- product_category MUST be one of:
{categories_str}
"""

    user_prompt = f"""
User scan:
{json.dumps(scan_data)}

User profile:
{json.dumps(profile)}

Generate skincare routine.

Return JSON:

{{
  "why": ["Oil control", "Pigment care", "Hydration boost", "Barrier repair"],
  "morning": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "repair",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "sunscreen",
      "focus": "protection"
    }}
  ],
  "night": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "repair",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "moisturizer",
      "focus": "hydration"
    }}
  ],
  "weekly": [
    {{
      "phase": "balance",
      "product_category": "cleanser",
      "focus": "oiliness"
    }},
    {{
      "phase": "maintenance",
      "product_category": "serum",
      "focus": "pigmentation"
    }},
    {{
      "phase": "maintenance",
      "product_category": "moisturizer",
      "focus": "hydration"
    }}
  ]
}}

IMPORTANT RULES:

- Always generate:
  - 4 morning steps
  - 4 night steps
  - 2 weekly care steps

- Weekly care must NEVER be empty.

- Use ONLY categories that exist in product database:
  cleanser
  serum
  moisturizer
  sunscreen
  mask

- Do NOT generate:
  toner
  essence
  ampoule
  exfoliator
  cream cleanser
  gel cleanser
  foam cleanser

- Weekly care should usually include:
  - clay mask
  - hydrating mask
  - exfoliating mas

"""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = "".join([b.text for b in response.content if b.type == "text"])
    clean = extract_json(raw)

    if not clean:
        raise Exception("Invalid AI response")

    return json.loads(clean)