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
        opt_bytes, _ = optimise_image(img, "image/jpeg", max_px=640)
        optimised_images.append(opt_bytes)


    encoded_images = [base64.b64encode(img).decode("utf-8") for img in optimised_images]

    system_prompt = """
You are a highly critical, precise clinical dermatology AI system. Act as an expert board-certified dermatologist.

STRICT CLINICAL RULES:
- Return ONLY valid JSON, no markdown code blocks, extra text, or explanations.
- Follow the schema exactly. All scores must be integers (0-100).
- Be highly critical, objective, and realistic. Do not be overly generous, polite, or optimistic. Evaluate the skin exactly as a doctor would.
- Under checked_area, visible_area, and hydration, a score of 100 represents perfect skin health (e.g. zero acne, zero redness, perfect hydration, perfect texture). Deduct points aggressively for any blemishes, wrinkles, redness, pores, or unevenness.
- If a condition is visible, its corresponding health/quality score must be significantly lower (e.g. 50-74 for mild/moderate issues, and <50 for severe issues).
- Ensure the overall_score is mathematically consistent with the sub-scores (e.g. if any detected condition has "Severe" severity, or if any checked area is <50, overall_score must be <60. Mild issues = 80-89, moderate = 60-79).
- checked_area: Return exactly 5 items. Rather than returning only the highest-scoring (healthiest) ones, return a balanced diagnostic overview: the 3 most critical/lowest-scoring (problematic) areas, and the 2 highest-scoring (healthiest) areas.
- visible_area: Must include only the single condition with the highest visible impact.
- detected_condition: Must include exactly 3 conditions from the allowed list.
- note: A clinical summary or tip of exactly 10-12 words.
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

3. Return exactly 5 items under checked_area: the 3 lowest-scoring (most problematic) areas, and the 2 highest-scoring (healthiest) areas. All scores must be health/quality metrics (100 = perfect health, 0 = severe condition).

4. visible_area condition must be ONE of:
acne, pimple, redness, irritation, pigmentation, dullness

5. visible_area must include ONLY the most dominant condition. visible_area score represents the health of that specific condition/area (100 = completely clear, 0 = severe).

6. areas must be selected from:
cheeks, nose, forehead, chin, under_eye

7. All scores must be integers (0-100)

8. No explanation. JSON only.

9. prognosis_timeline scores represent expected change in that area (positive means improvement, negative means worsening), Show the difference in score, not the final score.

10. Hydration target is the amount of water intake (in ml) recommended to reach optimal skin hydration based on the analysis.

11. Detected conditions MUST be chosen ONLY from this list: acne, blackheads, whiteheads, pores, oiliness, dryness, dehydration, redness, irritation, sensitivity, pigmentation, dark_spots, uneven_tone, dullness, dark_circles, eye_bags, fine_lines, wrinkles, loss_of_elasticity, sun_damage. Return exactly 3 items using exact names only.

12. OVERALL SCORE CALCULATION: Do not output a default or static number. Start at 100 and dynamically deduct points based on the severity of the detected conditions, hydration level, and checked areas. A completely clear face is 95+, mild issues 80-90, moderate 60-79, severe <60. Be highly dynamic.
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
        model="claude-haiku-4-5",
        max_tokens=800,
        temperature=0.4,
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
        print("[ERROR] CLAUDE RAW RESPONSE:", response)
        raise Exception(f"Claude parsing failed: {str(e)}")

async def verify_same_person(images: list[bytes]) -> dict:
    optimised_images = []
    for img in images:
        opt_bytes, _ = optimise_image(img, "image/jpeg", max_px=720)
        optimised_images.append(opt_bytes)

    encoded_images = [base64.b64encode(img).decode("utf-8") for img in optimised_images]

    system_prompt = """
You are an AI tasked with verifying if all provided images show the same person.
Return ONLY a JSON object in this exact format:
{
  "same_person": true or false,
  "reason": "brief reason"
}
"""

    user_prompt = "Do all these images show the exact same person? Return JSON."

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

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            temperature=0.4,
            system=system_prompt,
            messages=[{"role": "user", "content": content}]
        )

        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        clean_json = extract_json(raw_text)
        if clean_json:
            return json.loads(clean_json)
        return {"same_person": False, "reason": "Failed to parse AI response."}
    except Exception as e:
        print("ERROR: CLAUDE VERIFY ERROR:", e)
        return {"same_person": False, "reason": str(e)}