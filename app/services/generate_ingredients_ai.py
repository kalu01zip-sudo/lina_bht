from anthropic import Anthropic
import os
import json
import re

client = Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY")
)


def _extract_json_array(text: str):
    """Extract a JSON array from AI response text."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return []


async def generate_ingredients_ai(
    product_name: str,
    product_category: str
) -> list[str]:
    """
    Ask AI to generate a list of common ingredients
    for the given product name and category.
    Returns a list of ingredient strings.
    """

    prompt = f"""
Product Name: {product_name}
Product Category: {product_category}

Generate a JSON array of 5-10 common key ingredients 
for this skincare/haircare product.

Return ONLY a JSON array of strings, nothing else.
Example: ["Niacinamide", "Hyaluronic Acid", "Vitamin C"]
"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
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

        ingredients = _extract_json_array(raw)

        # Ensure all items are strings
        return [str(item) for item in ingredients]

    except Exception as e:
        print("[ERROR] generate_ingredients_ai:", e)
        return []
