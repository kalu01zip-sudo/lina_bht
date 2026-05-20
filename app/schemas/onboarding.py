from pydantic import BaseModel, field_validator
from typing import List, Optional, Literal
from datetime import date


# ---------------- PERSONAL INFO ----------------
class PersonalInfoRequest(BaseModel):
    country: str
    language: str
    date_of_birth: date
    gender: Literal["male", "female", "other"]  # ✅ restrict properly

    @field_validator("date_of_birth")
    def validate_dob(cls, value):
        if value > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return value


# ---------------- LIFE PHASE ----------------
class LifePhaseRequest(BaseModel):
    current_phase: Literal[
        "on my period",
        "pregnant",
        "postpartum",
        "menopause",
        "other",
        "none"
    ]
    custom_text: Optional[str] = None


# ---------------- ALLERGIES ----------------
class AllergiesRequest(BaseModel):
    allergies: List[str]
    custom_text: Optional[str] = None


# ---------------- SKIN + HAIR ----------------
class SkinHairRequest(BaseModel):
    # Skin
    skin_type: Literal[
        "dry", "combination", "normal", "oily", "sensitive"
    ]

    skin_concerns: List[str]
    skin_other: Optional[str] = None

    # Hair
    hair_type: Literal[
        "wavy", "straight", "curly", "coily"
    ]

    hair_concerns: List[str]
    hair_other: Optional[str] = None

# ---------------- BUDGET ----------------
class BudgetRequest(BaseModel):
    budget: Literal[
        "budget-friendly",
        "mid-range",
        "premium"
    ]