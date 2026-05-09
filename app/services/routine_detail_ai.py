from anthropic import Anthropic
import os
import json
import re

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


def extract_json(text: str):

    match = re.search(r"\{.*\}", text, re.DOTALL)

    return match.group() if match else None


async def generate_routine_detail_ai(
    routine: dict
):

    prompt = f"""
Routine Product:

{json.dumps(routine)}

Generate JSON ONLY:

{{
  "reading_duration": "2-3 min",

  "text": "40-50 words skincare educational explanation",

  "key_benefits": [
    "5-6 words",
    "5-6 words",
    "5-6 words",
    "5-6 words"
  ],

  "what_you_learn": [
    "5-6 words",
    "5-6 words",
    "5-6 words",
    "5-6 words"
  ]
}}
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    raw = "".join([
        x.text for x in response.content
        if x.type == "text"
    ])

    clean = extract_json(raw)

    return json.loads(clean)