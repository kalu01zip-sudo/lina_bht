# routers/auth.py
"""
All auth endpoints — MongoDB version with Google OAuth2.

Endpoints:
  POST /auth/signup              → register with email+password
  POST /auth/verify-email        → verify email OTP
  POST /auth/resend-otp          → resend OTP
  POST /auth/signin              → login → tokens
  POST /auth/google              → Google Sign-In (mobile)
  POST /auth/refresh             → refresh access token
  POST /auth/signout             → logout (revoke token)
  POST /auth/forgot-password     → send reset OTP
  POST /auth/reset-password      → reset password with OTP
  POST /auth/change-password     → change password (auth required)
  GET  /auth/me                  → get current user
  PUT  /auth/me                  → update profile
"""

import os
from datetime import datetime
from typing import Annotated, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.database import users_col, otp_col, tokens_col
from app.core.auth_utils import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_access_token, decode_refresh_token,
    generate_otp, otp_expiry, refresh_expiry,
    send_verification_email, send_reset_email,
)
from app.schemas import (
    SignUpRequest, SignInRequest, VerifyEmailRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
    ChangePasswordRequest, RefreshTokenRequest,
    ResendOTPRequest, GoogleAuthRequest,
    ProfileUpdateRequest, MessageResponse,
    OnboardingRequest,
)

from app.security.apple_auth import verify_apple_token
from app.schemas import AppleAuthRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])
bearer = HTTPBearer()

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


# ─────────────────────────────────────────────────
#  REVENUECAT HELPER
# ─────────────────────────────────────────────────

async def _create_rc_customer(user_id: str):
    """
    Silently create a RevenueCat customer right after a new user is created.
    Uses MongoDB _id as the RC customer ID so the mobile SDK can identify
    the user on first launch with Purchases.configure(..., appUserID: userId).
    Non-fatal — signup is never blocked if RC is unreachable.
    """
    try:
        rc_v2_key  = os.getenv("REVENUECAT_V2_API_KEY", "")
        project_id = os.getenv("REVENUECAT_PROJECT_ID", "")

        if not rc_v2_key or not project_id:
            print("⚠️  RC not configured — skipping customer creation.")
            return

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"https://api.revenuecat.com/v2/projects/{project_id}/customers",
                headers={
                    "Authorization": f"Bearer {rc_v2_key}",
                    "Content-Type":  "application/json",
                },
                json={"id": user_id},
            )
        if resp.status_code in (200, 201):
            print(f"✅ RC customer created: {user_id}")
        else:
            print(f"⚠️  RC customer creation returned {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"⚠️  RC customer creation failed (non-fatal): {e}")


# ─────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────

def _fmt(user: dict) -> dict:
    """Format MongoDB user document for API response."""
    return {
        "id":                   str(user["_id"]),
        "email":                user.get("email", ""),
        "full_name":            user.get("full_name"),
        "is_verified":          user.get("is_verified", False),
        "auth_provider":        user.get("auth_provider", "email"),
        "avatar_url":           user.get("avatar_url"),
        "apple_id":             user.get("apple_id"),
        # ── Onboarding / skin profile ──────────────────────────────
        "onboarding_completed": user.get("onboarding_completed", False),
        "skin_type":            user.get("skin_type"),
        "hair_type":            user.get("hair_type"),
        "current_phase":        user.get("current_phase"),
        "skin_concerns":        user.get("skin_concerns", []),
        "hair_concerns":        user.get("hair_concerns", []),
        "allergies":            user.get("allergies", []),
        "budget":               user.get("budget"),          # budget_friendly | midrange | premium
        # ──────────────────────────────────────────────────────────
        "created_at":           user.get("created_at", datetime.utcnow()).isoformat(),
        "plan":                 user.get("plan", "free"),    # "free" | "premium"
    }


def _tokens(user: dict) -> dict:
    uid = str(user["_id"])
    return {
        "access_token":  create_access_token(uid, user["email"]),
        "refresh_token": create_refresh_token(uid),
    }


async def _save_refresh_token(user_id: str, token: str):
    await tokens_col().insert_one({
        "user_id":    user_id,
        "token":      token,
        "is_revoked": False,
        "expires_at": refresh_expiry(),
        "created_at": datetime.utcnow(),
    })


async def _get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> dict:
    payload = decode_access_token(cred.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await users_col().find_one({"_id": ObjectId(payload["sub"])})
    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="User not found or deactivated.")
    return user


CurrentUser = Annotated[dict, Depends(_get_current_user)]


# ─────────────────────────────────────────────────
#  SIGN UP
# ─────────────────────────────────────────────────

@router.post("/signup", status_code=201)
async def signup(body: SignUpRequest):
    """Register a new account. Sends 6-digit OTP to email."""

    if await users_col().find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    result = await users_col().insert_one({
        "email":                body.email,
        "full_name":            body.full_name,
        "hashed_password":      hash_password(body.password),
        "auth_provider":        "email",
        "is_verified":          False,
        "is_active":            True,
        "google_id":            None,
        "avatar_url":           None,
        # ── Onboarding fields (filled via POST /auth/onboarding) ──
        "onboarding_completed": False,
        "skin_type":            None,
        "hair_type":            None,
        "current_phase":        None,
        "skin_concerns":        [],
        "hair_concerns":        [],
        "allergies":            [],
        "budget":               None,
        # ──────────────────────────────────────────────────────────
        "created_at":           datetime.utcnow(),
        "updated_at":           datetime.utcnow(),
        "last_login_at":        None,
    })

    otp = generate_otp()
    await otp_col().insert_one({
        "email":      body.email,
        "code":       otp,
        "purpose":    "verify_email",
        "is_used":    False,
        "expires_at": otp_expiry(),
        "created_at": datetime.utcnow(),
    })

    send_verification_email(body.email, body.full_name or "", otp)

    # ── Auto-create RC customer (non-fatal) ──────────────────────────────────
    await _create_rc_customer(str(result.inserted_id))

    return {
        "success": True,
        "message": "Account created! Check your email for the 6-digit verification code.",
    }


# ─────────────────────────────────────────────────
#  VERIFY EMAIL
# ─────────────────────────────────────────────────

@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest):
    """Verify email with the OTP sent at signup."""

    otp_doc = await otp_col().find_one({
        "email":      body.email,
        "code":       body.otp,
        "purpose":    "verify_email",
        "is_used":    False,
        "expires_at": {"$gt": datetime.utcnow()},
    })
    if not otp_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code.")

    await otp_col().update_one({"_id": otp_doc["_id"]}, {"$set": {"is_used": True}})
    await users_col().update_one(
        {"email": body.email},
        {"$set": {"is_verified": True, "updated_at": datetime.utcnow()}}
    )
    return {"success": True, "message": "Email verified! You can now sign in."}


# ─────────────────────────────────────────────────
#  RESEND OTP
# ─────────────────────────────────────────────────

@router.post("/resend-otp")
async def resend_otp(body: ResendOTPRequest):
    """Resend a new OTP code."""
    user = await users_col().find_one({"email": body.email})
    if user:
        otp = generate_otp()
        await otp_col().insert_one({
            "email":      body.email,
            "code":       otp,
            "purpose":    body.purpose,
            "is_used":    False,
            "expires_at": otp_expiry(),
            "created_at": datetime.utcnow(),
        })
        if body.purpose == "reset_password":
            send_reset_email(body.email, otp)
        else:
            send_verification_email(body.email, user.get("full_name", ""), otp)

    # Always return success (prevent email enumeration)
    return {"success": True, "message": "A new code has been sent to your email."}


# ─────────────────────────────────────────────────
#  SIGN IN (Email + Password)
# ─────────────────────────────────────────────────

@router.post("/signin")
async def signin(body: SignInRequest):
    """Sign in → returns access token + refresh token + user."""

    user = await users_col().find_one({"email": body.email})

    if not user or not user.get("hashed_password"):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account deactivated.")

    if not user.get("is_verified", False):
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please check your email for the OTP code.",
        )

    tok = _tokens(user)
    await _save_refresh_token(str(user["_id"]), tok["refresh_token"])
    await users_col().update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login_at": datetime.utcnow()}}
    )

    return {
        "success": True,
        **tok,
        "token_type": "bearer",
        "user": _fmt(user),
    }


# ─────────────────────────────────────────────────
#  GOOGLE SIGN-IN / SIGN-UP
#
#  Flow (React Native side):
#    1. User taps "Sign in with Google"
#    2. Google Sign-In SDK returns an id_token
#    3. App sends id_token to POST /auth/google
#    4. Backend verifies it with Google's API
#    5. Creates or finds existing user → returns tokens
#
#  Required: no extra secret needed — Google verifies via tokeninfo endpoint
# ─────────────────────────────────────────────────

@router.post("/google")
async def google_signin(body: GoogleAuthRequest):
    """
    Authenticate with Google (mobile Google Sign-In).

    React Native setup:
      npm install @react-native-google-signin/google-signin
      GoogleSignin.configure({ webClientId: 'YOUR_WEB_CLIENT_ID' })
      const { idToken } = await GoogleSignin.signIn()
      axios.post('/auth/google', { id_token: idToken })
    """
    # Verify token with Google
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={body.id_token}"
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token. Please try again.")

    g = resp.json()

    # Validate the token belongs to a real Google account
    if g.get("error_description"):
        raise HTTPException(status_code=401, detail=f"Google token error: {g['error_description']}")

    google_id  = g.get("sub")
    email      = g.get("email")
    full_name  = g.get("name")
    avatar_url = g.get("picture")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Could not retrieve Google account info.")

    # Find existing user by google_id OR email
    user = await users_col().find_one({
        "$or": [{"google_id": google_id}, {"email": email}]
    })

    if user:
        # Link Google ID if signing into an existing email account
        update = {
            "google_id":     google_id,
            "is_verified":   True,
            "last_login_at": datetime.utcnow(),
            "updated_at":    datetime.utcnow(),
        }
        if not user.get("avatar_url") and avatar_url:
            update["avatar_url"] = avatar_url
        await users_col().update_one({"_id": user["_id"]}, {"$set": update})
        user = await users_col().find_one({"_id": user["_id"]})

    else:
        # New user — create account automatically (no password needed)
        result = await users_col().insert_one({
            "email":           email,
            "full_name":       full_name,
            "hashed_password": None,
            "auth_provider":   "google",
            "is_verified":     True,        # Google accounts are pre-verified
            "is_active":       True,
            "google_id":       google_id,
            "avatar_url":      avatar_url,
            "skin_type":       None,
            "hair_type":       None,
            "current_phase":   None,
            "skin_concerns":   [],
            "hair_concerns":   [],
            "allergies":       [],
            "created_at":      datetime.utcnow(),
            "updated_at":      datetime.utcnow(),
            "last_login_at":   datetime.utcnow(),
        })
        user = await users_col().find_one({"_id": result.inserted_id})

        # ── Auto-create RC customer (non-fatal) ──────────────────────────────
        await _create_rc_customer(str(result.inserted_id))

    tok = _tokens(user)
    await _save_refresh_token(str(user["_id"]), tok["refresh_token"])

    return {
        "success": True,
        **tok,
        "token_type":   "bearer",
        "is_new_user":  user.get("auth_provider") == "google" and not user.get("skin_type"),
        "user":         _fmt(user),
    }


# ─────────────────────────────────────────────────
#  APPLE SIGN-IN / SIGN-UP
#
#  Flow (React Native side):
#    1. User taps "Sign in with Apple"
#    2. Apple returns identity_token + optional full_name
#    3. App sends both to POST /auth/apple
#    4. Backend verifies token with Apple's public keys
#    5. Creates or finds existing user → returns tokens
#
#  ⚠️ Apple only sends full_name on the VERY FIRST login.
#     Your app must forward it that one time and you must save it.
# ─────────────────────────────────────────────────

@router.post("/apple")
async def apple_signin(body: AppleAuthRequest):
    """
    Authenticate with Apple Sign-In (mobile).

    React Native setup:
      npm install @invertase/react-native-apple-authentication
      const appleAuthRequestResponse = await appleAuth.performRequest({
        requestedOperation: appleAuth.Operation.LOGIN,
        requestedScopes: [appleAuth.Scope.EMAIL, appleAuth.Scope.FULL_NAME],
      })
      axios.post('/auth/apple', {
        identity_token: appleAuthRequestResponse.identityToken,
        full_name: appleAuthRequestResponse.fullName?.givenName + ' ' + 
                   appleAuthRequestResponse.fullName?.familyName
      })
    """

    # ── DEV MOCK ──────────────────────────────────
    if os.environ.get("MOCK_MODE") == "true":
        return {
            "success":     True,
            "access_token":  "mock-apple-access-token",
            "refresh_token": "mock-apple-refresh-token",
            "token_type":    "bearer",
            "is_new_user":   True,
            "user": {
                "id":            "mock-apple-user-id",
                "email":         "appleuser@privaterelay.appleid.com",
                "full_name":     body.full_name or "Apple User",
                "auth_provider": "apple",
            }
        }
    # ──────────────────────────────────────────────

    # Step 1 — Verify token with Apple
    try:
        apple_data = await verify_apple_token(body.identity_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    apple_id = apple_data.get("sub")       # unique Apple user ID
    email    = apple_data.get("email")     # may be None on repeat logins

    if not apple_id:
        raise HTTPException(status_code=400, detail="Could not retrieve Apple account info.")

    # Step 2 — Find existing user by apple_id or email
    query = {"apple_id": apple_id}
    if email:
        query = {"$or": [{"apple_id": apple_id}, {"email": email}]}

    user = await users_col().find_one(query)

    if user:
        # Update apple_id if signing in via email match
        update = {
            "apple_id":      apple_id,
            "is_verified":   True,
            "last_login_at": datetime.utcnow(),
            "updated_at":    datetime.utcnow(),
        }
        # Save name only if not already set and Apple sent it
        if not user.get("full_name") and body.full_name and body.full_name.strip():
            update["full_name"] = body.full_name.strip()

        await users_col().update_one({"_id": user["_id"]}, {"$set": update})
        user = await users_col().find_one({"_id": user["_id"]})

    else:
        # New user — create account
        if not email:
            raise HTTPException(
                status_code=400,
                detail="Email not provided by Apple. Please try signing in again."
            )

        result = await users_col().insert_one({
            "email":                email,
            "full_name":            body.full_name.strip() if body.full_name else None,
            "hashed_password":      None,
            "auth_provider":        "apple",
            "is_verified":          True,
            "is_active":            True,
            "google_id":            None,
            "apple_id":             apple_id,
            "avatar_url":           None,
            # ── Onboarding fields ──────────────────────────────────
            "onboarding_completed": False,
            "skin_type":            None,
            "hair_type":            None,
            "current_phase":        None,
            "skin_concerns":        [],
            "hair_concerns":        [],
            "allergies":            [],
            "budget":               None,
            # ──────────────────────────────────────────────────────
            "created_at":           datetime.utcnow(),
            "updated_at":           datetime.utcnow(),
            "last_login_at":        datetime.utcnow(),
        })
        user = await users_col().find_one({"_id": result.inserted_id})

        # ── Auto-create RC customer (non-fatal) ──────────────────────────────
        await _create_rc_customer(str(result.inserted_id))

    tok = _tokens(user)
    await _save_refresh_token(str(user["_id"]), tok["refresh_token"])

    return {
        "success":      True,
        **tok,
        "token_type":   "bearer",
        "is_new_user":  user.get("auth_provider") == "apple" and not user.get("skin_type"),
        "user":         _fmt(user),
    }

# ─────────────────────────────────────────────────
#  REFRESH TOKEN
# ─────────────────────────────────────────────────

@router.post("/refresh")
async def refresh(body: RefreshTokenRequest):
    """Exchange refresh token for a new access + refresh token pair."""

    payload = decode_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    rt = await tokens_col().find_one({
        "token":      body.refresh_token,
        "is_revoked": False,
        "expires_at": {"$gt": datetime.utcnow()},
    })
    if not rt:
        raise HTTPException(status_code=401, detail="Refresh token revoked or expired.")

    user = await users_col().find_one({"_id": ObjectId(rt["user_id"])})
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")

    # Rotate — revoke old, issue new
    await tokens_col().update_one({"_id": rt["_id"]}, {"$set": {"is_revoked": True}})
    tok = _tokens(user)
    await _save_refresh_token(str(user["_id"]), tok["refresh_token"])

    return {"success": True, **tok, "token_type": "bearer", "user": _fmt(user)}


# ─────────────────────────────────────────────────
#  SIGN OUT
# ─────────────────────────────────────────────────

@router.post("/signout")
async def signout(body: RefreshTokenRequest):
    """Revoke refresh token — logs out this device."""
    await tokens_col().update_one(
        {"token": body.refresh_token},
        {"$set": {"is_revoked": True}}
    )
    return {"success": True, "message": "Signed out successfully."}


# ─────────────────────────────────────────────────
#  FORGOT PASSWORD
# ─────────────────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    """Send password reset OTP to email."""
    user = await users_col().find_one({"email": body.email})

    if user and user.get("auth_provider") == "email":
        otp = generate_otp()
        await otp_col().insert_one({
            "email":      body.email,
            "code":       otp,
            "purpose":    "reset_password",
            "is_used":    False,
            "expires_at": otp_expiry(),
            "created_at": datetime.utcnow(),
        })
        send_reset_email(body.email, otp)

    # Always success — prevent email enumeration
    return {"success": True, "message": "If this email is registered, a reset code has been sent."}


# ─────────────────────────────────────────────────
#  RESET PASSWORD
# ─────────────────────────────────────────────────

@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    """Reset password using the OTP from the reset email."""

    otp_doc = await otp_col().find_one({
        "email":      body.email,
        "code":       body.otp,
        "purpose":    "reset_password",
        "is_used":    False,
        "expires_at": {"$gt": datetime.utcnow()},
    })
    if not otp_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code.")

    user = await users_col().find_one({"email": body.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Update password and revoke all refresh tokens (security)
    await users_col().update_one(
        {"_id": user["_id"]},
        {"$set": {
            "hashed_password": hash_password(body.new_password),
            "updated_at":      datetime.utcnow(),
        }}
    )
    await otp_col().update_one({"_id": otp_doc["_id"]}, {"$set": {"is_used": True}})
    await tokens_col().update_many(
        {"user_id": str(user["_id"]), "is_revoked": False},
        {"$set": {"is_revoked": True}}
    )

    return {"success": True, "message": "Password reset! Please sign in with your new password."}


# ─────────────────────────────────────────────────
#  CHANGE PASSWORD  (auth required)
# ─────────────────────────────────────────────────

@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, current_user: CurrentUser):
    """Change password for the currently logged-in user."""
    if not current_user.get("hashed_password"):
        raise HTTPException(status_code=400, detail="Google accounts cannot use email password.")

    if not verify_password(body.current_password, current_user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    await users_col().update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "hashed_password": hash_password(body.new_password),
            "updated_at":      datetime.utcnow(),
        }}
    )
    return {"success": True, "message": "Password changed successfully."}


# ─────────────────────────────────────────────────
#  GET CURRENT USER
# ─────────────────────────────────────────────────

@router.get("/me")
async def get_me(current_user: CurrentUser):
    """Return the current authenticated user's full profile."""
    return {"success": True, "user": _fmt(current_user)}


# ─────────────────────────────────────────────────
#  UPDATE PROFILE  (auth required)
# ─────────────────────────────────────────────────

# @router.put("/me")
# async def update_profile(body: ProfileUpdateRequest, current_user: CurrentUser):
#     """Update the logged-in user's SkinSense profile (partial update — send only fields to change)."""
#     updates = {"updated_at": datetime.utcnow()}

#     if body.full_name      is not None: updates["full_name"]      = body.full_name
#     if body.skin_type      is not None: updates["skin_type"]      = body.skin_type
#     if body.hair_type      is not None: updates["hair_type"]      = body.hair_type
#     if body.current_phase  is not None: updates["current_phase"]  = body.current_phase
#     if body.skin_concerns  is not None: updates["skin_concerns"]  = body.skin_concerns
#     if body.hair_concerns  is not None: updates["hair_concerns"]  = body.hair_concerns
#     if body.allergies      is not None: updates["allergies"]      = body.allergies
#     if body.budget         is not None: updates["budget"]         = body.budget

#     await users_col().update_one({"_id": current_user["_id"]}, {"$set": updates})
#     updated = await users_col().find_one({"_id": current_user["_id"]})

#     return {"success": True, "message": "Profile updated.", "user": _fmt(updated)}


# ─────────────────────────────────────────────────
#  ONBOARDING  (auth required)
# ─────────────────────────────────────────────────

# @router.post("/onboarding")
# async def submit_onboarding(body: OnboardingRequest, current_user: CurrentUser):
#     """
#     Submit onboarding answers after signup. Can be called once or multiple times
#     (re-submitting overwrites previous answers).

#     Mobile flow:
#       1. User signs up  →  POST /auth/signup
#       2. User verifies email  →  POST /auth/verify-email
#       3. App shows onboarding screens  →  POST /auth/onboarding
#       4. App checks `onboarding_completed` on GET /auth/me to decide
#          whether to show onboarding or go straight to dashboard.

#     Fields:
#       current_phase   : hormonal/life phase (optional — user may skip)
#       has_allergies   : if false, allergies list is saved as []
#       allergies       : list of known allergens
#       skin_type       : primary skin type
#       skin_concerns   : one or more skin concerns
#       hair_type       : hair texture
#       hair_concerns   : one or more hair/scalp concerns
#       budget          : preferred product price range
#     """
#     # If user said no allergies, always store empty list
#     allergies = (body.allergies or []) if body.has_allergies else []

#     updates = {
#         "onboarding_completed": True,
#         "current_phase":        body.current_phase,
#         "skin_type":            body.skin_type,
#         "skin_concerns":        body.skin_concerns,
#         "hair_type":            body.hair_type,
#         "hair_concerns":        body.hair_concerns,
#         "allergies":            allergies,
#         "budget":               body.budget,
#         "updated_at":           datetime.utcnow(),
#     }

#     await users_col().update_one({"_id": current_user["_id"]}, {"$set": updates})
#     updated = await users_col().find_one({"_id": current_user["_id"]})

#     return {
#         "success": True,
#         "message": "Onboarding complete! Your skin profile has been saved.",
#         "user":    _fmt(updated),
#     }

@router.delete("/me")
async def delete_me(current_user: CurrentUser):
    """
    Permanently delete current user account.
    """

    user_id = str(current_user["_id"])

    # revoke refresh tokens
    await tokens_col().update_many(
        {"user_id": user_id},
        {"$set": {"is_revoked": True}}
    )

    # delete OTP history
    await otp_col().delete_many({
        "email": current_user["email"]
    })

    # delete user
    await users_col().delete_one({
        "_id": current_user["_id"]
    })

    return {
        "success": True,
        "message": "Account deleted successfully."
    }