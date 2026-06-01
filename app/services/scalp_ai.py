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

async def analyze_scalp_with_claude(image_bytes: bytes, allowed_conditions: list[str] = None):
    # Optimise image
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg", max_px=640)
    encoded_image = base64.b64encode(opt_bytes).decode("utf-8")

    system_prompt = """
You are a highly critical, precise clinical trichology AI system. Act as an expert board-certified trichologist specializing in scalp and hair health.

STRICT CLINICAL RULES:
- Return ONLY valid JSON, no markdown code blocks, extra text, or explanations.
- Follow the schema exactly. All scores must be integers (0-100).
- Be highly critical, objective, and realistic. Do not be overly generous, polite, or optimistic. Evaluate the scalp and hair exactly as a doctor would.
- Under checked_area, visible_area, and scalp_health, a score of 100 represents perfect scalp/hair health (e.g. zero dandruff, zero oiliness, perfect hydration/shine, no thinning). Deduct points aggressively for any visible issues.
- If a condition is visible, its corresponding health/quality score must be significantly lower (e.g. 50-74 for mild/moderate issues, and <50 for severe issues).
- Ensure the overall_score and scalp_health are mathematically consistent with the sub-scores (e.g. if any detected condition has "Severe" severity, or if any checked area is <50, overall_score and scalp_health must be <60. Mild issues = 80-89, moderate = 60-79).
- checked_area: Return exactly 5 items. Rather than returning only the highest-scoring (healthiest) ones, return a balanced diagnostic overview: the 3 most critical/lowest-scoring (problematic) areas, and the 2 highest-scoring (healthiest) areas.
- visible_area: Must include only the single condition with the highest visible impact.
- detected_condition: Must include exactly 3 conditions from the allowed list.
- note: A clinical summary or tip of exactly 10-12 words.
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

2. prognosis_timeline name and score means in 7 and 14 days, which checked_area will improve or worsen, and by how much (score change). Return only 2 items per timeline. All prognosis_timeline scores represent expected change in that area (positive means improvement, negative means worsening). Show the difference in score, not the final score.

3. Return exactly 5 items under checked_area: the 3 lowest-scoring (most problematic) areas, and the 2 highest-scoring (healthiest) areas. All scores must be health/quality metrics (100 = perfect health, 0 = severe condition).

4. visible_area condition must be ONE of:
dandruff, oiliness, redness, inflammation, thinning, buildup

5. areas must be selected from:
top, crown, sides, hairline

6. All scores must be integers (0-100)

7. No explanation. JSON only.

8. Detected conditions MUST be chosen ONLY from this list: dandruff, oily_scalp, dry_scalp, redness, inflammation, sensitivity, product_buildup, hair_thinning, hair_loss, split_ends, brittle_hair, scalp_acne. Return exactly 3 items using exact names only.

9. OVERALL SCORE AND SCALP HEALTH CALCULATION: Do not output a default or static number. Start at 100 and dynamically deduct points based on the severity of the detected conditions, checked areas, and visible issues. A completely clear scalp is 95+, mild issues 80-90, moderate 60-79, severe <60. Be highly dynamic. If any detected condition has "Severe" severity, or if any checked area is <50, overall_score and scalp_health must be <60.
"""
    if not allowed_conditions:
        allowed_conditions = ["dandruff", "oily_scalp", "dry_scalp", "redness", "inflammation", "sensitivity", "product_buildup", "hair_thinning", "hair_loss", "split_ends", "brittle_hair", "scalp_acne"]
    conditions_str = ", ".join(allowed_conditions)
    user_prompt = user_prompt.replace(
        "dandruff, oily_scalp, dry_scalp, redness, inflammation, sensitivity, product_buildup, hair_thinning, hair_loss, split_ends, brittle_hair, scalp_acne",
        conditions_str
    )


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
        model="claude-haiku-4-5",
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


async def validate_scalp_or_hair_image(image_bytes: bytes) -> dict:
    opt_bytes, _ = optimise_image(image_bytes, "image/jpeg", max_px=512, quality=70)
    encoded_image = base64.b64encode(opt_bytes).decode("utf-8")

    system_prompt = """
You are a strict image gatekeeper for a scalp and hair scan endpoint.

Return ONLY valid JSON.
Do not include markdown or extra text.
"""

    user_prompt = """
Check whether the image clearly contains human scalp, hair, hairline, or close-up hair strands.

Return ONLY this JSON:
{
  "scalp_or_hair_detected": boolean,
  "reason": "short reason"
}

Reject images that are products, faces without visible hair/scalp focus, full body photos,
objects, documents, animals, drawings, random backgrounds, or unclear images.
"""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=80,
        temperature=0,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
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
        }]
    )

    try:
        raw_text = "".join([block.text for block in response.content if hasattr(block, "text")])
        clean_json = extract_json(raw_text)
        if not clean_json:
            raise Exception("Invalid JSON from Claude")
        data = json.loads(clean_json)
        return {
            "scalp_or_hair_detected": bool(data.get("scalp_or_hair_detected")),
            "reason": data.get("reason") or "Image does not clearly show scalp or hair."
        }
    except Exception as e:
        raise Exception(f"Scalp validation parsing failed: {str(e)}")
