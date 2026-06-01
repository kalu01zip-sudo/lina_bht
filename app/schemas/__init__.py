import re
from typing import Optional, List, Literal
from pydantic import BaseModel, EmailStr, field_validator


def _strong_password(v: str) -> str:
    if len(v) < 8:            raise ValueError("Min 8 characters.")
    if not re.search(r"[A-Za-z]", v): raise ValueError("Must contain a letter.")
    if not re.search(r"\d", v):       raise ValueError("Must contain a number.")
    return v


# ── Auth ───────────────────────────────────────────────────────────────────
class SignUpRequest(BaseModel):
    email:     EmailStr
    password:  str
    full_name: Optional[str] = None
    onesignal_id: Optional[str] = None
    _val_pw = field_validator("password")(_strong_password)

    model_config = {"json_schema_extra": {"example": {
        "email": "user@example.com", "password": "Secure123", "full_name": "Jane Doe", "onesignal_id": "onesignal-sub-id-123"
    }}}

class SignInRequest(BaseModel):
    email:    EmailStr
    password: str
    onesignal_id: Optional[str] = None
    model_config = {"json_schema_extra": {"example": {
        "email": "user@example.com", "password": "Secure123", "onesignal_id": "onesignal-sub-id-123"
    }}}

class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp:   str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    email:        EmailStr
    otp:          str
    new_password: str
    _val_pw = field_validator("new_password")(_strong_password)

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str
    _val_pw = field_validator("new_password")(_strong_password)

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class ResendOTPRequest(BaseModel):
    email:   EmailStr
    purpose: str = "verify_email"

# Google OAuth
class GoogleAuthRequest(BaseModel):
    id_token: str   # Google ID token from mobile app (Firebase / Google Sign-In SDK)
    onesignal_id: Optional[str] = None
    model_config = {"json_schema_extra": {"example": {"id_token": "eyJhbGciOiJSUzI1NiIs...", "onesignal_id": "onesignal-sub-id-123"}}}

# ── Profile ───────────────────────────────────────────────────────────────

# Valid onboarding values — shared between OnboardingRequest and ProfileUpdateRequest
PhaseType        = Literal["on_my_period", "pregnant", "postpartum", "menopause"]
AllergyType      = Literal[
    "fragrance", "parabens", "formaldehyde", "phenoxyethanol",
    "retinol", "salicylic_acid", "benzoyl_peroxide", "alcohol_denat",
    "oxybenzone", "nickel", "sulfates", "alcohol",
]
SkinTypeEnum     = Literal["dry", "combination", "normal", "oily", "sensitive"]
SkinConcernType  = Literal["acne_pimple", "irritation_redness", "pigmentation", "dullness"]
HairTypeEnum     = Literal["wavy", "straight", "curly", "coily_kinky"]
HairConcernType  = Literal["hair_fall", "dandruff", "oily_scalp", "dry_scalp"]
BudgetType       = Literal["budget_friendly", "midrange", "premium"]


class OnboardingRequest(BaseModel):
    """
    Submitted once after signup to complete the user's skin/hair profile.
    All fields are required — the mobile app must collect all answers before submitting.

    current_phase   : hormonal/life phase (optional — user may skip)
    has_allergies   : true → allergies list is populated; false → allergies = []
    allergies       : list of known allergens (empty when has_allergies=false)
    skin_type       : user's primary skin type
    skin_concerns   : one or more visible skin concerns
    hair_type       : user's hair texture
    hair_concerns   : one or more hair/scalp concerns
    budget          : preferred product price range
    """
    current_phase:  Optional[PhaseType]          = None
    has_allergies:  bool                          = False
    allergies:      Optional[List[AllergyType]]  = None
    skin_type:      SkinTypeEnum
    skin_concerns:  List[SkinConcernType]         = []
    hair_type:      HairTypeEnum
    hair_concerns:  List[HairConcernType]         = []
    budget:         BudgetType                    = "midrange"

    model_config = {"json_schema_extra": {"example": {
        "current_phase":  "on_my_period",
        "has_allergies":  True,
        "allergies":      ["fragrance", "parabens"],
        "skin_type":      "oily",
        "skin_concerns":  ["acne_pimple", "dullness"],
        "hair_type":      "curly",
        "hair_concerns":  ["dandruff", "oily_scalp"],
        "budget":         "midrange",
    }}}


class ProfileUpdateRequest(BaseModel):
    """Used by PUT /auth/me — all fields are optional (partial update)."""
    full_name:      Optional[str]                 = None
    skin_type:      Optional[SkinTypeEnum]        = None
    hair_type:      Optional[HairTypeEnum]        = None
    current_phase:  Optional[PhaseType]           = None
    skin_concerns:  Optional[List[SkinConcernType]] = None
    hair_concerns:  Optional[List[HairConcernType]] = None
    allergies:      Optional[List[AllergyType]]   = None
    budget:         Optional[BudgetType]          = None

    model_config = {"json_schema_extra": {"example": {
        "full_name":     "Jane Doe",
        "skin_type":     "oily",
        "hair_type":     "curly",
        "current_phase": "on_my_period",
        "skin_concerns": ["acne_pimple", "dullness"],
        "hair_concerns": ["dandruff"],
        "allergies":     ["fragrance"],
        "budget":        "midrange",
    }}}


# ── Responses ────────────────────────────────────────────────────────────
class MessageResponse(BaseModel):
    success: bool
    message: str

class AppleAuthRequest(BaseModel):
    identity_token: str        # JWT from Apple
    full_name: Optional[str] = None   # only sent on FIRST login
    onesignal_id: Optional[str] = None


# ── RevenueCat Subscription ─────────────────────────────────────────────
class VerifySubscriptionRequest(BaseModel):
    """
    Sent by mobile after a purchase is completed via RevenueCat SDK.
    app_user_id must equal the MongoDB user _id (set in RC SDK on login).
    """
    app_user_id: str

class CancelSubscriptionRequest(BaseModel):
    """Not used server-side (cancellation is done in the store).
    Kept for any future admin use."""
    reason: Optional[str] = None
