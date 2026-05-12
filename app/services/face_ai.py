from anthropic import Anthropic
import base64
import os
import json
import re

# ✅ INIT CLIENT FIRST
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# SAFE JSON EXTRACTOR
def extract_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end+1]
    return None


from app.utils.image_utils import optimise_image

# ✅ MAIN FUNCTION (ONLY ONE)
async def analyze_face_with_claude(images: list[bytes]):

    optimised_images = []
    for img in images:
        opt_bytes, _ = optimise_image(img, "image/jpeg")
        optimised_images.append(opt_bytes)

    encoded_images = [base64.b64encode(img).decode("utf-8") for img in optimised_images]

    system_prompt = """
You are an advanced dermatology AI system.

STRICT RULES:
- Return ONLY valid JSON
- Do NOT include explanation, markdown, or extra text
- Follow the schema exactly
- All scores must be integers (0-100)
- Use ONLY allowed names where specified
- If unsure, estimate based on visible evidence

IMPORTANT:
- checked_area names MUST be selected ONLY from the provided list
- Return ONLY top 5 highest scoring checked_area items
- visible_area must include ONLY ONE condition (highest visible impact)
- detected_condition must include exactly 3 items
- note must be 10-12 words only
"""

    user_prompt = """
Analyze the provided face images.

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
    "condition": "acne | pimple | redness | irritation | pigmentation | dullness",
    "areas": ["cheeks", "nose", "forehead", "chin", "under_eye"],
    "score": int
  },

  "hydration": int,

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
    "stress_score": int,
    "water_intake": int,
    "sleep_quality": int
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
  },

  "hydration_target": int
}

IMPORTANT RULES:

1. checked_area names MUST be selected ONLY from this list:

hydration, sebum, redness, texture, evenness, pore_size, acne, blackheads, whiteheads, pigmentation, hyperpigmentation, dark_spots, sun_spots, freckles, melasma, dark_circles, eye_bags, fine_lines, wrinkles, crow_feet, elasticity, firmness, sagging, skin_tone, undertone, tone_uniformity, brightness, dullness, radiance, glow, sensitivity, inflammation, irritation, barrier_health, dryness, oil_balance, combination_zones, t_zone_oiliness, cheek_dryness, uv_damage, sun_damage, photoaging, collagen_level, skin_age, biological_age, oxidative_stress, pollution_damage, dehydration_risk, acne_risk, sensitivity_risk, aging_score, overall_skin_health, skin_recovery_rate, wound_healing, microbiome_balance

2. prognosis_timeline name and score means in 7 and 14 days, which checked_area will improve or worsen, and by how much (score change). Return only 2 items per timeline, selected from the checked_area list.

3. Return ONLY top 5 highest scoring checked_area

4. visible_area condition must be ONE of:
acne, pimple, redness, irritation, pigmentation, dullness

5. visible_area must include ONLY the most dominant condition

6. areas must be selected from:
cheeks, nose, forehead, chin, under_eye

7. All scores must be integers (0-100)

8. No explanation. JSON only.

9. prognosis_timeline scores represent expected change in that area (positive means improvement, negative means worsening), Show the difference in score, not the final score.

10. Hydration target is the amoount of water intake (in ml) recommended to reach optimal skin hydration based on the analysis.

11. Detected conditions MUST be chosen ONLY from this list: acne, blackheads, whiteheads, pores, oiliness, dryness, dehydration, redness, irritation, sensitivity, pigmentation, dark_spots, uneven_tone, dullness, dark_circles, eye_bags, fine_lines, wrinkles, loss_of_elasticity, sun_damage. Return exactly 3 items using exact names only.
"""

    # ✅ BUILD CONTENT
    content = []

    for img in encoded_images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img
            }
        })

    content.append({
        "type": "text",
        "text": user_prompt
    })

    # ✅ CALL CLAUDE
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": content
            }
        ]
    )

    # 🔥 ROBUST PARSING (IMPORTANT FIX)
    try:
        raw_text = ""

        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        if not raw_text:
            raise Exception("Empty Claude response")

        clean_json = extract_json(raw_text)

        if not clean_json:
            raise Exception(f"Invalid JSON from Claude: {raw_text}")

        return json.loads(clean_json)

    except Exception as e:
        print("❌ CLAUDE RAW RESPONSE:", response)
        raise Exception(f"Claude parsing failed: {str(e)}")