from anthropic import Anthropic
import base64
import os
import json
from app.utils.image_utils import optimise_image

# INIT CLIENT
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def extract_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end+1]
    return None

async def analyze_scalp_with_claude(image_bytes: bytes):
    # Optimise image
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg")
    encoded_image = base64.b64encode(opt_bytes).decode("utf-8")

    system_prompt = """
You are an advanced trichology AI system specializing in scalp and hair health.

STRICT RULES:
- Return ONLY valid JSON
- Do NOT include explanation, markdown, or extra text
- Follow the schema exactly
- All scores must be integers (0-100)
- Use ONLY allowed names where specified
- Return ONLY top 5 highest scoring checked_area items
- detected_condition must include exactly 3 items
- note must be 10-12 words only
"""

    user_prompt = """
Analyze the provided scalp/hair image.

Return ONLY JSON in this exact format:

{
  "overall_score": int,

  "checked_area": {
    "name": score,
    "name": score,
    "name": score,
    "name": score,
    "name": score
  },

  "visible_area": {
    "condition": "dandruff | oiliness | redness | inflammation | thinning | buildup",
    "areas": ["top", "crown", "sides", "hairline"],
    "score": int
  },

  "scalp_health": int,

  "detected_condition": [
    {
      "name": string,
      "note": "10-12 words sentence",
      "severity": "Mild | Moderate | Severe"
    },
    {
      "name": string,
      "note": "10-12 words sentence",
      "severity": "Mild | Moderate | Severe"
    },
    {
      "name": string,
      "note": "10-12 words sentence",
      "severity": "Mild | Moderate | Severe"
    }
  ],

  "lifestyle_factor": {
    "stress_impact": int,
    "hygiene_score": int,
    "dietary_factor": int
  },

  "prognosis_timeline": {
    "seven_days": {
      "name": score,
      "name": score
    },
    "fourteen_days": {
      "name": score,
      "name": score
    }
  }
}

IMPORTANT RULES:

1. checked_area names MUST be selected ONLY from this list:
dandruff, oiliness, dryness, redness, inflammation, sensitivity, buildup, folliculitis, thinning, hair_density, hair_texture, shine, breakage, split_ends, scalp_elasticity, follicles_health, sebum_level, hydration, microbiome_balance, fungal_activity

2. prognosis_timeline name and score means in 7 and 14 days, which checked_area will improve or worsen, and by how much (score change). Return only 2 items per timeline.

3. Return ONLY top 5 highest scoring checked_area

4. visible_area condition must be ONE of:
dandruff, oiliness, redness, inflammation, thinning, buildup

5. areas must be selected from:
top, crown, sides, hairline

6. All scores must be integers (0-100)

7. No explanation. JSON only.

8. Detected conditions MUST be chosen ONLY from this list: dandruff, oily_scalp, dry_scalp, redness, inflammation, sensitivity, product_buildup, hair_thinning, hair_loss, split_ends, brittle_hair, scalp_acne. Return exactly 3 items.
"""

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

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": content}]
    )

    try:
        raw_text = "".join([block.text for block in response.content if hasattr(block, "text")])
        clean_json = extract_json(raw_text)
        if not clean_json:
            raise Exception("Invalid JSON from Claude")
        return json.loads(clean_json)
    except Exception as e:
        raise Exception(f"Scalp analysis parsing failed: {str(e)}")
