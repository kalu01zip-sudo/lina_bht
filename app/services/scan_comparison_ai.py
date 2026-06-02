from anthropic import Anthropic
import os
import json
import logging

logger = logging.getLogger(__name__)

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

async def generate_comparison_message(scan_1_analysis: dict, scan_2_analysis: dict) -> str:
    """
    Generates a 20-25 word comparison message between two scans.
    """
    is_scalp = "scalp_health" in scan_1_analysis or "scalp_health" in scan_2_analysis

    if is_scalp:
        system_prompt = "You are a trichology AI. Compare two scalp and hair analysis results and provide a brief, professional observation."
        user_prompt = f"""
    Compare these two scalp and hair scans for the same user and summarize the progress or changes in one sentence.
    
    Scan 1 Analysis: {json.dumps(scan_1_analysis)}
    Scan 2 Analysis: {json.dumps(scan_2_analysis)}
    
    Rules:
    - Exactly ONE sentence.
    - 20-25 words long.
    - Focus on the most significant change (score, condition, or scalp health).
    - Be professional and encouraging.
    - Do NOT mention "Scan 1" or "Scan 2", use "your scalp and hair" or "the results".
    """
        fallback_msg = "Your scalp and hair health has evolved positively between these two assessments, with visible improvements in scalp condition and hair vitality supporting your routine."
    else:
        system_prompt = "You are a dermatology AI. Compare two skin analysis results and provide a observation in brief, professional language."
        user_prompt = f"""
    Compare these two skin scans for the same user and summarize the progress or changes in one sentence.
    
    Scan 1 Analysis: {json.dumps(scan_1_analysis)}
    Scan 2 Analysis: {json.dumps(scan_2_analysis)}
    
    Rules:
    - Exactly ONE sentence.
    - 20-25 words long.
    - Focus on the most significant change (score, condition, or hydration).
    - Be professional and encouraging.
    - Do NOT mention "Scan 1" or "Scan 2", use "your skin" or "the results".
    """
        fallback_msg = "Your skin health has evolved positively between these two assessments, with visible improvements in hydration and overall texture supporting your routine."

    try:
        # Using a faster model if possible, or sticking to sonnet for quality
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            temperature=0.7,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        
        # Strip potential quotes
        text = text.strip().strip('"')
        
        return text
    except Exception as e:
        logger.error("Comparison AI Error: %s", e)
        # Fallback message
        return fallback_msg
