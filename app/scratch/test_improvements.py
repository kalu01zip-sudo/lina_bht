# app/scratch/test_improvements.py
import sys
import os

# Ensure the app package is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.core.mapping import normalize_condition, resolve_condition, extract_nutrition
from app.services.face_ai import _enforce_score_consistency as face_enforce
from app.services.scalp_ai import _enforce_score_consistency as scalp_enforce
from app.core.recommender import boost_by_context

def test_normalization():
    print("Running normalization tests...")
    # Test normalization of space and hyphen
    assert normalize_condition("oily scalp") == "oily_scalp"
    assert normalize_condition("oily-scalp") == "oily_scalp"
    assert normalize_condition("  OILY scalp  ") == "oily_scalp"
    
    # Test resolve_condition with alias
    assert resolve_condition("seborrheic dermatitis") == "acne"
    assert resolve_condition("seborrheic_dermatitis") == "acne"
    assert resolve_condition("hyperpigmentation") == "pigmentation"
    
    print("Normalization tests passed!")

def test_ai_post_processing():
    print("Running AI post-processing normalization tests...")
    
    # Face AI data normalization
    face_raw = {
        "overall_score": 75,
        "hydration": 60,
        "detected_condition": [
            {"name": "oily scalp", "severity": "Mild"},
            {"name": "acne", "severity": "Moderate"}
        ],
        "score_breakdown": {
            "deductions": [
                {"condition": "oily scalp", "severity": "Mild", "penalty": 5},
                {"condition": "acne", "severity": "Moderate", "penalty": 18}
            ]
        }
    }
    
    face_clean = face_enforce(face_raw)
    assert face_clean["detected_condition"][0]["name"] == "oily_scalp"
    assert face_clean["score_breakdown"]["deductions"][0]["condition"] == "oily_scalp"

    # Scalp AI data normalization
    scalp_raw = {
        "overall_score": 75,
        "scalp_health": 80,
        "detected_condition": [
            {"name": "dry scalp", "severity": "Severe"}
        ],
        "score_breakdown": {
            "deductions": [
                {"condition": "dry scalp", "severity": "Severe", "penalty": 26}
            ]
        }
    }
    
    scalp_clean = scalp_enforce(scalp_raw)
    assert scalp_clean["detected_condition"][0]["name"] == "dry_scalp"
    assert scalp_clean["score_breakdown"]["deductions"][0]["condition"] == "dry_scalp"
    
    print("AI post-processing normalization tests passed!")

def test_recommender_boosting():
    print("Running recommender boost tests...")
    
    # Test case 1: Skin scan - low hydration. Item with hydration tag should get +5
    ai_data_skin = {
        "hydration": 40,
        "detected_condition": []
    }
    item_hydration = {"tags": ["hydration"]}
    item_other = {"tags": ["glow"]}
    assert boost_by_context(item_hydration, ai_data_skin) == 5
    assert boost_by_context(item_other, ai_data_skin) == 0

    # Test case 2: Scalp scan - low scalp health. Item with scalp tag/condition should get +5
    ai_data_scalp = {
        "scalp_health": 45,
        "detected_condition": []
    }
    item_scalp = {"tags": ["dandruff"]}
    item_hair = {"tags": ["hair"]}
    item_skin = {"tags": ["hydration"]}
    assert boost_by_context(item_scalp, ai_data_scalp) == 5
    assert boost_by_context(item_hair, ai_data_scalp) == 5
    assert boost_by_context(item_skin, ai_data_scalp) == 0

    # Test case 3: Severity boost - targeted matching
    ai_data_severity = {
        "hydration": 80,
        "detected_condition": [
            {"name": "acne", "severity": "Severe"},
            {"name": "dandruff", "severity": "Moderate"}
        ]
    }
    # Item that targets acne should get +5
    item_acne = {"tags": ["acne"]}
    # Item that targets dandruff should get +3
    item_dandruff = {"detected_condition": "dandruff"}
    # Item that targets glow should get 0
    item_glow = {"tags": ["glow"]}
    
    assert boost_by_context(item_acne, ai_data_severity) == 5
    assert boost_by_context(item_dandruff, ai_data_severity) == 3
    assert boost_by_context(item_glow, ai_data_severity) == 0

    print("Recommender boost tests passed!")

if __name__ == "__main__":
    test_normalization()
    test_ai_post_processing()
    test_recommender_boosting()
    print("All scratch tests passed successfully!")
