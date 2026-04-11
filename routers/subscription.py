# routers/subscription.py
"""
Subscription endpoints (RevenueCat):

  POST /subscription/verify   → called by mobile after a purchase; backend
                                 checks the user's entitlements on RevenueCat
                                 and marks them premium in MongoDB.

  GET  /subscription/status   → return the current plan/status from MongoDB.

  POST /subscription/cancel   → RevenueCat / App Store / Play Store do not
                                 allow server-side cancellation. This endpoint
                                 returns a deep-link so the user can cancel
                                 through the store themselves.

  POST /subscription/webhook  → RevenueCat webhook handler (INITIAL_PURCHASE,
                                 RENEWAL, CANCELLATION, EXPIRATION,
                                 BILLING_ISSUE, …).

How the flow works
──────────────────
1. Mobile app uses the RevenueCat SDK to make a purchase.
2. RevenueCat SDK validates the receipt with Apple / Google and grants
   the entitlement on their side automatically.
3. Mobile calls  POST /subscription/verify  with the user's RC app_user_id
   (we use the MongoDB _id string as the RC app_user_id — set this in the SDK).
4. Backend calls RevenueCat REST API, reads the entitlement, and updates
   the subscription document in MongoDB.
5. RevenueCat also sends webhooks for every lifecycle event (renewal,
   cancellation, billing issue, etc.) so the DB stays in sync automatically.

.env keys required
──────────────────
  REVENUECAT_API_KEY            (secret key, starts with sk_...)
  REVENUECAT_WEBHOOK_AUTH_TOKEN (set in RC Dashboard → Webhooks → Auth header)
  RC_ENTITLEMENT_ID             (e.g. "premium")  — defaults to "premium"
"""

import os
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from typing import Annotated, Optional

from database import users_col, subscriptions_col
from auth_utils import decode_access_token
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId
from pydantic import BaseModel

router = APIRouter(prefix="/subscription", tags=["Subscription"])
bearer = HTTPBearer()

# ── RevenueCat config ────────────────────────────────────────────────────────

RC_API_KEY            = os.environ.get("REVENUECAT_API_KEY", "")
RC_WEBHOOK_AUTH_TOKEN = os.environ.get("REVENUECAT_WEBHOOK_AUTH_TOKEN", "")
RC_ENTITLEMENT_ID     = os.environ.get("RC_ENTITLEMENT_ID", "premium")

RC_BASE_URL = "https://api.revenuecat.com/v1"


# ── Schemas ──────────────────────────────────────────────────────────────────

class VerifySubscriptionRequest(BaseModel):
    """
    The mobile app sends the RevenueCat app_user_id after a purchase.
    We use the MongoDB _id string as the RC app_user_id — make sure the
    RC SDK is initialised with  Purchases.configure(..., appUserID: userId).
    """
    app_user_id: str          # MongoDB _id string == RC app_user_id


# ── Auth helper ──────────────────────────────────────────────────────────────

async def _require_auth(
    cred: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]
) -> dict:
    payload = decode_access_token(cred.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = await users_col().find_one({"_id": ObjectId(payload["sub"])})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


# ── Internal: call RevenueCat REST API ───────────────────────────────────────

async def _fetch_rc_subscriber(app_user_id: str) -> dict:
    """
    GET /v1/subscribers/{app_user_id}
    Returns the full subscriber object from RevenueCat.
    Raises HTTPException on network or API errors.
    """
    url     = f"{RC_BASE_URL}/subscribers/{app_user_id}"
    headers = {
        "Authorization": f"Bearer {RC_API_KEY}",
        "Content-Type":  "application/json",
        "X-Platform":    "ios",   # required by RC REST API
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 404:
        # RC doesn't know this user yet (no purchase made)
        return {}
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"RevenueCat API error {resp.status_code}: {resp.text[:200]}"
        )

    return resp.json().get("subscriber", {})


def _parse_entitlement(subscriber: dict) -> Optional[dict]:
    """
    Pull the target entitlement out of the RC subscriber dict.
    Returns None if the user has no active entitlement.
    """
    entitlements = subscriber.get("entitlements", {})
    ent = entitlements.get(RC_ENTITLEMENT_ID)
    if not ent:
        return None

    # expires_date is None for lifetime purchases; treat as active if present
    expires_str = ent.get("expires_date")
    if expires_str:
        expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        if expires_dt < datetime.now(timezone.utc):
            return None   # entitlement exists but has already expired

    return ent


def _ts_to_dt(iso_str: Optional[str]) -> Optional[datetime]:
    """Convert an ISO-8601 string (possibly None) to a naive UTC datetime."""
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.replace(tzinfo=None)   # store naive UTC in MongoDB (consistent with existing docs)


# ─────────────────────────────────────────────────────────────────────────────
#  VERIFY SUBSCRIPTION
#  Called by mobile immediately after a successful purchase.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/verify")
async def verify_subscription(
    body:         VerifySubscriptionRequest,
    current_user: dict = Depends(_require_auth),
):
    """
    Verify that the authenticated user has an active RevenueCat entitlement
    and sync it into MongoDB.

    The mobile app should call this endpoint right after the SDK reports a
    successful purchase or restore.

    Expected body:
      { "app_user_id": "<MongoDB user _id>" }

    The app_user_id must match the logged-in user (we enforce this below).
    """

    user_id = str(current_user["_id"])

    # Safety check: the RC app_user_id must belong to the logged-in user
    if body.app_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="app_user_id does not match the authenticated user."
        )

    # ── 1. Fetch subscriber from RevenueCat ──────────────────────────────────
    subscriber = await _fetch_rc_subscriber(user_id)
    if not subscriber:
        raise HTTPException(
            status_code=404,
            detail="No RevenueCat subscriber found. Complete a purchase first."
        )

    # ── 2. Check entitlement ─────────────────────────────────────────────────
    entitlement = _parse_entitlement(subscriber)
    if not entitlement:
        raise HTTPException(
            status_code=402,
            detail="No active entitlement found. Please complete a purchase."
        )

    # ── 3. Extract subscription details ─────────────────────────────────────
    # RC stores the active subscription product ID in the entitlement
    product_id       = entitlement.get("product_identifier", "")
    expires_dt       = _ts_to_dt(entitlement.get("expires_date"))
    purchase_dt      = _ts_to_dt(entitlement.get("purchase_date"))
    is_trial         = entitlement.get("period_type") == "trial"
    will_renew       = not subscriber.get("subscriptions", {}).get(
                           product_id, {}
                       ).get("unsubscribe_detected_at")

    # Infer plan name from product_id (e.g. "skinsense_monthly" → "monthly")
    if "yearly" in product_id or "annual" in product_id:
        plan = "yearly"
    elif "monthly" in product_id:
        plan = "monthly"
    else:
        plan = "premium"   # fallback for lifetime or unrecognised product IDs

    # ── 4. Upsert subscription in MongoDB ────────────────────────────────────
    now = datetime.utcnow()

    existing = await subscriptions_col().find_one({"user_id": user_id})

    sub_data = {
        "user_id":              user_id,
        "rc_app_user_id":       user_id,
        "rc_entitlement_id":    RC_ENTITLEMENT_ID,
        "product_id":           product_id,
        "plan":                 plan,
        "status":               "trialing" if is_trial else "active",
        "is_trial":             is_trial,
        "will_renew":           will_renew,
        "expires_at":           expires_dt,
        "purchase_date":        purchase_dt,
        "cancel_at_period_end": not will_renew,
        "updated_at":           now,
    }

    if existing:
        await subscriptions_col().update_one(
            {"user_id": user_id},
            {"$set": sub_data}
        )
    else:
        sub_data["created_at"] = now
        await subscriptions_col().insert_one(sub_data)

    # ── 5. Mark user as premium ───────────────────────────────────────────────
    await users_col().update_one(
        {"_id": current_user["_id"]},
        {"$set": {"plan": "premium", "updated_at": now}}
    )

    return {
        "success":   True,
        "message":   "Subscription verified and activated." if not is_trial
                     else f"Free trial active until {expires_dt.strftime('%B %d, %Y') if expires_dt else 'trial end'}.",
        "plan":      plan,
        "status":    sub_data["status"],
        "is_trial":  is_trial,
        "expires_at": expires_dt.isoformat() if expires_dt else None,
        "will_renew": will_renew,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SUBSCRIPTION STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def subscription_status(current_user: dict = Depends(_require_auth)):
    """Return the current subscription status from MongoDB."""

    sub = await subscriptions_col().find_one(
        {"user_id": str(current_user["_id"])},
        sort=[("created_at", -1)]
    )

    if not sub:
        return {
            "success": True,
            "plan":    "free",
            "status":  "inactive",
        }

    return {
        "success":              True,
        "plan":                 sub.get("plan", "free"),
        "status":               sub.get("status"),
        "is_trial":             sub.get("is_trial", False),
        "expires_at":           sub["expires_at"].isoformat() if sub.get("expires_at") else None,
        "will_renew":           sub.get("will_renew", False),
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  CANCEL SUBSCRIPTION
#  RevenueCat / App Store / Play Store don't allow server-side cancellation.
#  We return a deep-link so the user can cancel through the store.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(current_user: dict = Depends(_require_auth)):
    """
    Subscriptions purchased through the App Store or Play Store can only be
    cancelled by the user inside the store settings. This endpoint confirms
    the user has an active subscription and returns the relevant store link.

    iOS  → https://apps.apple.com/account/subscriptions
    Android → https://play.google.com/store/account/subscriptions
    """

    sub = await subscriptions_col().find_one({
        "user_id": str(current_user["_id"]),
        "status":  {"$in": ["trialing", "active"]},
    })

    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found.")

    return {
        "success": True,
        "message": (
            "To cancel your subscription, please visit your store subscription settings. "
            "You'll keep premium access until your current billing period ends."
        ),
        "cancel_links": {
            "ios":     "https://apps.apple.com/account/subscriptions",
            "android": "https://play.google.com/store/account/subscriptions",
        },
        "expires_at": sub["expires_at"].isoformat() if sub.get("expires_at") else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  REVENUECAT WEBHOOK
#  Register this URL in the RC Dashboard → Project → Webhooks.
#  Set an Authorization header token and put it in REVENUECAT_WEBHOOK_AUTH_TOKEN.
#
#  Events handled:
#    INITIAL_PURCHASE  → grant premium
#    RENEWAL           → refresh expiry date
#    PRODUCT_CHANGE    → update plan
#    CANCELLATION      → mark cancel_at_period_end
#    EXPIRATION        → downgrade to free
#    BILLING_ISSUE     → flag billing problem (keep access for grace period)
#    UNCANCELLATION    → user re-enabled auto-renew
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def revenuecat_webhook(
    request:       Request,
    authorization: Optional[str] = Header(None),
):
    # ── Verify shared secret ─────────────────────────────────────────────────
    if RC_WEBHOOK_AUTH_TOKEN:
        expected = f"Bearer {RC_WEBHOOK_AUTH_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Invalid webhook authorization.")

    body  = await request.json()
    event = body.get("event", {})
    etype = event.get("type", "")

    # RevenueCat sends the app_user_id we set in the SDK (= MongoDB _id string)
    app_user_id = event.get("app_user_id", "")

    if not app_user_id:
        # Nothing we can do without an identifier
        return {"success": True}

    now = datetime.utcnow()

    # ── Parse common fields ───────────────────────────────────────────────────
    expiration_str  = event.get("expiration_at_ms")
    expiration_dt   = (
        datetime.utcfromtimestamp(int(expiration_str) / 1000)
        if expiration_str else None
    )

    product_id = event.get("product_id", "")
    if "yearly" in product_id or "annual" in product_id:
        plan = "yearly"
    elif "monthly" in product_id:
        plan = "monthly"
    else:
        plan = "premium"

    period_type = event.get("period_type", "NORMAL")   # NORMAL | TRIAL | INTRO

    # ── Handle events ─────────────────────────────────────────────────────────

    if etype in ("INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION"):
        # Grant / renew premium access
        status = "trialing" if period_type == "TRIAL" else "active"

        await subscriptions_col().update_one(
            {"rc_app_user_id": app_user_id},
            {"$set": {
                "plan":                 plan,
                "status":               status,
                "is_trial":             period_type == "TRIAL",
                "will_renew":           True,
                "cancel_at_period_end": False,
                "expires_at":           expiration_dt,
                "product_id":           product_id,
                "updated_at":           now,
            },
             "$setOnInsert": {
                "user_id":           app_user_id,
                "rc_app_user_id":    app_user_id,
                "rc_entitlement_id": RC_ENTITLEMENT_ID,
                "created_at":        now,
            }},
            upsert=True,
        )

        try:
            await users_col().update_one(
                {"_id": ObjectId(app_user_id)},
                {"$set": {"plan": "premium", "updated_at": now}}
            )
        except Exception:
            pass   # app_user_id may not be a valid ObjectId in edge cases

    elif etype == "PRODUCT_CHANGE":
        # User switched plan (e.g. monthly → yearly)
        await subscriptions_col().update_one(
            {"rc_app_user_id": app_user_id},
            {"$set": {
                "plan":       plan,
                "product_id": product_id,
                "updated_at": now,
            }}
        )

    elif etype == "CANCELLATION":
        # User cancelled — keep access until period ends
        await subscriptions_col().update_one(
            {"rc_app_user_id": app_user_id},
            {"$set": {
                "cancel_at_period_end": True,
                "will_renew":           False,
                "expires_at":           expiration_dt,
                "updated_at":           now,
            }}
        )

    elif etype == "EXPIRATION":
        # Subscription has fully expired — downgrade to free
        await subscriptions_col().update_one(
            {"rc_app_user_id": app_user_id},
            {"$set": {
                "status":     "expired",
                "will_renew": False,
                "updated_at": now,
            }}
        )
        try:
            await users_col().update_one(
                {"_id": ObjectId(app_user_id)},
                {"$set": {"plan": "free", "updated_at": now}}
            )
        except Exception:
            pass

    elif etype == "BILLING_ISSUE":
        # Payment failed — RC typically has a grace period before EXPIRATION
        await subscriptions_col().update_one(
            {"rc_app_user_id": app_user_id},
            {"$set": {
                "status":     "past_due",
                "updated_at": now,
            }}
        )
        print(f"⚠️  Billing issue for RC user: {app_user_id}")

    else:
        # Unhandled event type — log and ignore
        print(f"ℹ️  Unhandled RC webhook event: {etype} for user {app_user_id}")

    return {"success": True}