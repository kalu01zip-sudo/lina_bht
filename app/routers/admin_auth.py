# routers/admin_auth.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Admin Authentication                        ║
║                                                                  ║
║  Admins live in a SEPARATE 'admins' MongoDB collection.         ║
║  Completely isolated from the mobile app's 'users' collection.  ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /admin/auth/create-first-admin  → create admin account   ║
║                                          (ADMIN_CREATION_SECRET)║
║   POST /admin/auth/signin              → email+password → tokens║
║   POST /admin/auth/forgot-password     → send reset OTP         ║
║   POST /admin/auth/verify-otp          → validate OTP (no-op    ║
║                                          consume; just checks)  ║
║   POST /admin/auth/reset-password      → set new password       ║
║                                          (consumes OTP)         ║
║   POST /admin/auth/change-password     → change password        ║
║                                          (auth required)        ║
║   POST /admin/auth/refresh             → rotate token pair      ║
║   POST /admin/auth/signout             → revoke refresh token   ║
║   GET  /admin/auth/me                  → current admin profile  ║
╚══════════════════════════════════════════════════════════════════╝

.env required:
    ADMIN_CREATION_SECRET=<a strong random string>
        Used as the X-Admin-Creation-Secret header value when calling
        POST /admin/auth/create-first-admin.
        Without this, nobody can create an admin account.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, field_validator
import re

from app.core.database import get_db, otp_col, tokens_col
from app.core.auth_utils import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    generate_otp,
    otp_expiry,
    refresh_expiry,
    send_reset_email,
)

router = APIRouter(prefix="/admin/auth", tags=["Admin Authentication"])
_bearer = HTTPBearer()


# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _admins_col():
    """Returns the 'admins' collection — separate from 'users'."""
    return get_db()["admins"]


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
#  Defined here to keep all admin code in one place.
# ══════════════════════════════════════════════════════════════════════════════

def _strong_password(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Minimum 8 characters.")
    if not re.search(r"[A-Za-z]", v):
        raise ValueError("Must contain at least one letter.")
    if not re.search(r"\d", v):
        raise ValueError("Must contain at least one number.")
    return v


class AdminSignInRequest(BaseModel):
    email:    EmailStr
    password: str

    model_config = {"json_schema_extra": {"example": {
        "email":    "admin@skinsense.com",
        "password": "Admin123",
    }}}


class AdminForgotPasswordRequest(BaseModel):
    email: EmailStr

    model_config = {"json_schema_extra": {"example": {
        "email": "admin@skinsense.com",
    }}}


class AdminVerifyOtpRequest(BaseModel):
    email: EmailStr
    otp:   str

    model_config = {"json_schema_extra": {"example": {
        "email": "admin@skinsense.com",
        "otp":   "483920",
    }}}


class AdminResetPasswordRequest(BaseModel):
    email:        EmailStr
    otp:          str
    new_password: str

    _val_pw = field_validator("new_password")(_strong_password)

    model_config = {"json_schema_extra": {"example": {
        "email":        "admin@skinsense.com",
        "otp":          "483920",
        "new_password": "NewAdmin123",
    }}}


class AdminChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str

    _val_pw = field_validator("new_password")(_strong_password)

    model_config = {"json_schema_extra": {"example": {
        "current_password": "OldAdmin123",
        "new_password":     "NewAdmin456",
    }}}


class AdminRefreshTokenRequest(BaseModel):
    refresh_token: str


class AdminCreateRequest(BaseModel):
    """
    Body for POST /admin/auth/create-first-admin.
    The request must also include the X-Admin-Creation-Secret header.
    """
    email:     EmailStr
    password:  str
    full_name: Optional[str] = None

    _val_pw = field_validator("password")(_strong_password)

    model_config = {"json_schema_extra": {"example": {
        "email":     "admin@skinsense.com",
        "password":  "Admin123",
        "full_name": "Super Admin",
    }}}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_admin(admin: dict) -> dict:
    """Format an admin document for API responses."""
    return {
        "id":         str(admin["_id"]),
        "email":      admin.get("email", ""),
        "full_name":  admin.get("full_name"),
        "avatar_url": admin.get("avatar_url"),
        "is_active":  admin.get("is_active", True),
        "created_at": admin.get("created_at", datetime.utcnow()).isoformat(),
        "last_login_at": (
            admin["last_login_at"].isoformat()
            if admin.get("last_login_at")
            else None
        ),
    }


def _admin_tokens(admin: dict) -> dict:
    """Issue an access + refresh token pair for an admin."""
    uid = str(admin["_id"])
    return {
        "access_token":  create_access_token(uid, admin["email"]),
        "refresh_token": create_refresh_token(uid),
    }


async def _save_refresh_token(admin_id: str, token: str) -> None:
    """Persist a refresh token to the shared refresh_tokens collection."""
    await tokens_col().insert_one({
        "user_id":    admin_id,   # admin ObjectId stored as string
        "token":      token,
        "is_revoked": False,
        "expires_at": refresh_expiry(),
        "created_at": datetime.utcnow(),
    })


# ── Auth dependency ───────────────────────────────────────────────────────────

async def _get_current_admin(
    cred: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict:
    """
    Decode the Bearer JWT and look up the admin in the 'admins' collection.

    A token issued for a regular mobile user will be rejected here because
    the sub (ObjectId) will not exist in the 'admins' collection.
    Similarly, an admin token is rejected by the mobile _get_current_user
    dependency because the admin ObjectId is not in the 'users' collection.
    """
    payload = decode_access_token(cred.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        admin = await _admins_col().find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token subject.")

    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found.")
    if not admin.get("is_active", True):
        raise HTTPException(status_code=403, detail="Admin account is deactivated.")

    return admin


CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── Create first admin ────────────────────────────────────────────────────────

@router.post(
    "/create-first-admin",
    status_code = 201,
    summary     = "Create an admin account",
    description = (
        "Creates a new admin account in the `admins` collection.\n\n"
        "**Security gate:** the request must include the "
        "`X-Admin-Creation-Secret` header whose value matches the "
        "`ADMIN_CREATION_SECRET` environment variable. "
        "Without the correct secret this endpoint always returns 403.\n\n"
        "There is no limit on how many admins can be created — "
        "use the secret to control access.\n\n"
        "Duplicate emails are rejected with 409."
    ),
)
async def create_admin(
    body:   AdminCreateRequest,
    secret: Annotated[str, Header(alias="X-Admin-Creation-Secret")] = "",
):
    # ── 1. Validate secret ────────────────────────────────────────────────────
    expected = os.getenv("ADMIN_CREATION_SECRET", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "ADMIN_CREATION_SECRET is not configured on this server. "
                "Set it in your .env file before creating admin accounts."
            ),
        )
    if secret != expected:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing X-Admin-Creation-Secret header.",
        )

    # ── 2. Reject duplicate emails ────────────────────────────────────────────
    if await _admins_col().find_one({"email": body.email}):
        raise HTTPException(
            status_code=409,
            detail=f"An admin account with email '{body.email}' already exists.",
        )

    # ── 3. Insert admin document ──────────────────────────────────────────────
    result = await _admins_col().insert_one({
        "email":           body.email,
        "full_name":       body.full_name,
        "hashed_password": hash_password(body.password),
        "is_active":       True,
        "created_at":      datetime.utcnow(),
        "updated_at":      datetime.utcnow(),
        "last_login_at":   None,
    })

    admin = await _admins_col().find_one({"_id": result.inserted_id})

    return {
        "success": True,
        "message": f"Admin account created for {body.email}.",
        "admin":   _fmt_admin(admin),
    }


# ── Sign in ───────────────────────────────────────────────────────────────────

@router.post(
    "/signin",
    summary     = "Admin sign in",
    description = (
        "Authenticate with email + password against the `admins` collection.\n\n"
        "Returns an access token (short-lived) and a refresh token (long-lived). "
        "Include the access token as `Authorization: Bearer <token>` on all "
        "subsequent admin requests.\n\n"
        "Mobile user credentials are rejected — admins and users are stored "
        "in separate collections."
    ),
)
async def admin_signin(body: AdminSignInRequest):
    # ── 1. Find admin by email ────────────────────────────────────────────────
    admin = await _admins_col().find_one({"email": body.email})

    # Deliberately vague error — prevents email enumeration
    if not admin or not verify_password(body.password, admin.get("hashed_password", "")):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    if not admin.get("is_active", True):
        raise HTTPException(
            status_code=403,
            detail="This admin account has been deactivated.",
        )

    # ── 2. Issue tokens ───────────────────────────────────────────────────────
    tok = _admin_tokens(admin)
    await _save_refresh_token(str(admin["_id"]), tok["refresh_token"])

    # ── 3. Update last_login_at ───────────────────────────────────────────────
    await _admins_col().update_one(
        {"_id": admin["_id"]},
        {"$set": {"last_login_at": datetime.utcnow()}},
    )

    return {
        "success":      True,
        "token_type":   "bearer",
        **tok,
        "admin":        _fmt_admin(admin),
    }


# ── Forgot password ───────────────────────────────────────────────────────────

@router.post(
    "/forgot-password",
    summary     = "Send password reset OTP",
    description = (
        "Sends a 6-digit OTP to the admin's registered email address.\n\n"
        "Always returns HTTP 200 with a success message regardless of whether "
        "the email is registered — this prevents email enumeration.\n\n"
        "The OTP is stored in the shared `otp_codes` collection with "
        "`purpose: 'admin_reset_password'` and expires in 10 minutes "
        "(same TTL as mobile OTPs).\n\n"
        "**Flow:** forgot-password → verify-otp → reset-password"
    ),
)
async def admin_forgot_password(body: AdminForgotPasswordRequest):
    admin = await _admins_col().find_one({"email": body.email})

    if admin and admin.get("is_active", True):
        otp = generate_otp()
        await otp_col().insert_one({
            "email":      body.email,
            "code":       otp,
            "purpose":    "admin_reset_password",
            "is_used":    False,
            "expires_at": otp_expiry(),
            "created_at": datetime.utcnow(),
        })
        # Reuse the existing reset email template
        send_reset_email(body.email, otp)

    # Always return the same response — prevents email enumeration
    return {
        "success": True,
        "message": "If this email is registered, a reset code has been sent.",
    }


# ── Verify OTP ────────────────────────────────────────────────────────────────

@router.post(
    "/verify-otp",
    summary     = "Verify password reset OTP",
    description = (
        "Validates the 6-digit OTP sent by `POST /admin/auth/forgot-password`.\n\n"
        "**Does NOT consume the OTP** — it stays valid so the next step "
        "(`POST /admin/auth/reset-password`) can verify and consume it.\n\n"
        "Use this endpoint to confirm the OTP is correct before showing the "
        "'set new password' form in your dashboard UI.\n\n"
        "Returns `verified: true` on success, HTTP 400 if the OTP is invalid "
        "or expired."
    ),
)
async def admin_verify_otp(body: AdminVerifyOtpRequest):
    otp_doc = await otp_col().find_one({
        "email":      body.email,
        "code":       body.otp,
        "purpose":    "admin_reset_password",
        "is_used":    False,
        "expires_at": {"$gt": datetime.utcnow()},
    })

    if not otp_doc:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OTP code.",
        )

    return {
        "success":  True,
        "verified": True,
        "message":  "OTP verified. You may now set a new password.",
    }


# ── Reset password ────────────────────────────────────────────────────────────

@router.post(
    "/reset-password",
    summary     = "Reset password using OTP",
    description = (
        "Set a new password using the OTP from `POST /admin/auth/forgot-password`.\n\n"
        "**Consumes the OTP** — it cannot be reused after this call.\n\n"
        "Also revokes all existing refresh tokens for this admin, forcing "
        "re-authentication on all devices.\n\n"
        "**Flow:** forgot-password → verify-otp → **reset-password**"
    ),
)
async def admin_reset_password(body: AdminResetPasswordRequest):
    # ── 1. Validate and consume OTP ───────────────────────────────────────────
    otp_doc = await otp_col().find_one({
        "email":      body.email,
        "code":       body.otp,
        "purpose":    "admin_reset_password",
        "is_used":    False,
        "expires_at": {"$gt": datetime.utcnow()},
    })
    if not otp_doc:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset code.",
        )

    # ── 2. Find the admin ─────────────────────────────────────────────────────
    admin = await _admins_col().find_one({"email": body.email})
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found.")

    # ── 3. Update password ────────────────────────────────────────────────────
    await _admins_col().update_one(
        {"_id": admin["_id"]},
        {"$set": {
            "hashed_password": hash_password(body.new_password),
            "updated_at":      datetime.utcnow(),
        }},
    )

    # ── 4. Mark OTP as used ───────────────────────────────────────────────────
    await otp_col().update_one(
        {"_id": otp_doc["_id"]},
        {"$set": {"is_used": True}},
    )

    # ── 5. Revoke all refresh tokens for this admin (security) ───────────────
    await tokens_col().update_many(
        {"user_id": str(admin["_id"]), "is_revoked": False},
        {"$set": {"is_revoked": True}},
    )

    return {
        "success": True,
        "message": "Password reset successfully. Please sign in with your new password.",
    }


# ── Change password (auth required) ──────────────────────────────────────────

@router.post(
    "/change-password",
    summary     = "Change password (authenticated)",
    description = (
        "Change the password for the currently signed-in admin.\n\n"
        "Requires a valid `Authorization: Bearer <access_token>` header.\n\n"
        "Unlike `reset-password`, this requires the **current password** to be "
        "provided — the admin must be signed in and know their existing password.\n\n"
        "Does **not** revoke existing refresh tokens so other devices stay logged in."
    ),
)
async def admin_change_password(
    body:          AdminChangePasswordRequest,
    current_admin: CurrentAdmin,
):
    # ── 1. Verify current password ────────────────────────────────────────────
    if not verify_password(body.current_password, current_admin.get("hashed_password", "")):
        raise HTTPException(
            status_code=400,
            detail="Current password is incorrect.",
        )

    # ── 2. Reject same-as-current password ────────────────────────────────────
    if verify_password(body.new_password, current_admin.get("hashed_password", "")):
        raise HTTPException(
            status_code=400,
            detail="New password must be different from the current password.",
        )

    # ── 3. Update ─────────────────────────────────────────────────────────────
    await _admins_col().update_one(
        {"_id": current_admin["_id"]},
        {"$set": {
            "hashed_password": hash_password(body.new_password),
            "updated_at":      datetime.utcnow(),
        }},
    )

    return {
        "success": True,
        "message": "Password changed successfully.",
    }


# ── Refresh tokens ────────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    summary     = "Rotate admin token pair",
    description = (
        "Exchange a valid refresh token for a new access + refresh token pair.\n\n"
        "The old refresh token is revoked immediately (rotation).\n\n"
        "Call this when the access token expires (typically after 15–30 min) "
        "to keep the admin session alive without re-entering credentials."
    ),
)
async def admin_refresh(body: AdminRefreshTokenRequest):
    # ── 1. Decode refresh token ───────────────────────────────────────────────
    payload = decode_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    # ── 2. Validate against DB ────────────────────────────────────────────────
    rt = await tokens_col().find_one({
        "token":      body.refresh_token,
        "is_revoked": False,
        "expires_at": {"$gt": datetime.utcnow()},
    })
    if not rt:
        raise HTTPException(status_code=401, detail="Refresh token revoked or expired.")

    # ── 3. Find admin ─────────────────────────────────────────────────────────
    try:
        admin = await _admins_col().find_one({"_id": ObjectId(rt["user_id"])})
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token subject.")

    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found.")
    if not admin.get("is_active", True):
        raise HTTPException(status_code=403, detail="Admin account is deactivated.")

    # ── 4. Rotate — revoke old, issue new ─────────────────────────────────────
    await tokens_col().update_one({"_id": rt["_id"]}, {"$set": {"is_revoked": True}})
    tok = _admin_tokens(admin)
    await _save_refresh_token(str(admin["_id"]), tok["refresh_token"])

    return {
        "success":    True,
        "token_type": "bearer",
        **tok,
        "admin":      _fmt_admin(admin),
    }


# ── Sign out ──────────────────────────────────────────────────────────────────

@router.post(
    "/signout",
    summary     = "Admin sign out",
    description = (
        "Revoke the provided refresh token, logging out the current device.\n\n"
        "Other active sessions (other refresh tokens) are **not** affected.\n\n"
        "To log out all devices at once, use `POST /admin/auth/reset-password` "
        "or manually revoke all tokens via MongoDB."
    ),
)
async def admin_signout(body: AdminRefreshTokenRequest):
    await tokens_col().update_one(
        {"token": body.refresh_token},
        {"$set": {"is_revoked": True}},
    )
    return {"success": True, "message": "Signed out successfully."}


# ── Get current admin ─────────────────────────────────────────────────────────

@router.get(
    "/me",
    summary     = "Get current admin profile",
    description = (
        "Returns the profile of the currently authenticated admin.\n\n"
        "Requires `Authorization: Bearer <access_token>`."
    ),
)
async def admin_me(current_admin: CurrentAdmin):
    return {"success": True, "admin": _fmt_admin(current_admin)}
