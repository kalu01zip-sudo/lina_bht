from pydantic import BaseModel, field_validator
from typing import List, Optional, Literal
from datetime import date

class PersonalInfoRequest(BaseModel):
    country: str
    language: str
    date_of_birth: date
    gender: str

    @field_validator("date_of_birth")
    def validate_dob(cls, value):
        if value > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return value
    
class LifePhaseRequest(BaseModel):
    current_phase: Literal[
        "on my period",
        "pregnant",
        "postpartum",
        "menopause",
        "other"
    ]
    custom_text: Optional[str] = None