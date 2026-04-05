# schemas.py
import re
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


def _strong_password(v: str) -> str:
    if len(v) < 8:            raise ValueError("Min 8 characters.")
    if not re.search(r"[A-Za-z]", v): raise ValueError("Must contain a letter.")
    if not re.search(r"\d", v):       raise ValueError("Must contain a number.")
    return v


# ── Auth ──────────────────────────────────────────────────────
class SignUpRequest(BaseModel):
    email:     EmailStr
    password:  str
    full_name: Optional[str] = None
    _val_pw = field_validator("password")(_strong_password)

    model_config = {"json_schema_extra": {"example": {
        "email": "user@example.com", "password": "Secure123", "full_name": "Jane Doe"
    }}}

class SignInRequest(BaseModel):
    email:    EmailStr
    password: str
    model_config = {"json_schema_extra": {"example": {
        "email": "user@example.com", "password": "Secure123"
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
    model_config = {"json_schema_extra": {"example": {"id_token": "eyJhbGciOiJSUzI1NiIs..."}}}

# ── Profile ───────────────────────────────────────────────────
class ProfileUpdateRequest(BaseModel):
    full_name:      Optional[str]       = None
    skin_type:      Optional[str]       = None
    hair_type:      Optional[str]       = None
    current_phase:  Optional[str]       = None
    skin_concerns:  Optional[list[str]] = None
    hair_concerns:  Optional[list[str]] = None
    allergies:      Optional[list[str]] = None

    model_config = {"json_schema_extra": {"example": {
        "full_name": "Jane Doe", "skin_type": "oily", "hair_type": "curly",
        "current_phase": "on_my_period", "skin_concerns": ["acne", "redness"],
        "hair_concerns": ["dandruff"], "allergies": ["perfumes"],
    }}}

# ── Responses ─────────────────────────────────────────────────
class MessageResponse(BaseModel):
    success: bool
    message: str

class AppleAuthRequest(BaseModel):
    identity_token: str        # JWT from Apple
    full_name: Optional[str] = None   # only sent on FIRST login

#stripe
class CreateSubscriptionRequest(BaseModel):
    plan:             str   # "monthly" | "yearly"
    payment_method_id: str  # from Stripe.js on frontend e.g. "pm_xxx"

class CancelSubscriptionRequest(BaseModel):
    reason: Optional[str] = None