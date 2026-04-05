# routers/subscription.py
"""
Subscription endpoints:
  POST /subscription/create     → start trial + subscription
  POST /subscription/cancel     → cancel at period end
  GET  /subscription/status     → get current plan
  POST /subscription/webhook    → Stripe webhook handler
"""

import os
from re import sub
import stripe
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from typing import Annotated, Optional

from database import users_col, subscriptions_col
from auth_utils import decode_access_token
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId

router = APIRouter(prefix="/subscription", tags=["Subscription"])
bearer = HTTPBearer()

# ── Stripe config ────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

PRICE_IDS = {
    "monthly": os.environ.get("STRIPE_MONTHLY_PRICE_ID", ""),
    "yearly":  os.environ.get("STRIPE_YEARLY_PRICE_ID",  ""),
}

TRIAL_DAYS = 7


# ── Auth helper ──────────────────────────────────

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


# ─────────────────────────────────────────────────
#  CREATE SUBSCRIPTION (with 7-day free trial)
# ─────────────────────────────────────────────────

from schemas import CreateSubscriptionRequest

@router.post("/create")
async def create_subscription(
    body:         CreateSubscriptionRequest,
    current_user: dict = Depends(_require_auth),
):
    plan              = body.plan
    payment_method_id = body.payment_method_id
    """
    Start a subscription with 7-day free trial.
    Card is saved but NOT charged until trial ends.

    Frontend sends:
      plan:              "monthly" or "yearly"
      payment_method_id: from Stripe.js confirmCardSetup or createPaymentMethod
    """

    if plan not in PRICE_IDS:
        raise HTTPException(status_code=400, detail="Plan must be 'monthly' or 'yearly'.")

    user_id = str(current_user["_id"])

    # Check if already subscribed
    existing = await subscriptions_col().find_one({
        "user_id": user_id,
        "status":  {"$in": ["trialing", "active"]}
    })
    if existing:
        raise HTTPException(status_code=409, detail="You already have an active subscription.")

    # Check if already used free trial
    used_trial = await subscriptions_col().find_one({
        "user_id":    user_id,
        "trial_used": True,
    })
    trial_days = TRIAL_DAYS if not used_trial else 0

    try:
        # 1 — Create or retrieve Stripe customer
        stripe_customer_id = current_user.get("stripe_customer_id")
        if not stripe_customer_id:
            customer = stripe.Customer.create(
                email    = current_user["email"],
                name     = current_user.get("full_name", ""),
                metadata = {"user_id": user_id},
            )
            stripe_customer_id = customer.id
            await users_col().update_one(
                {"_id": current_user["_id"]},
                {"$set": {"stripe_customer_id": stripe_customer_id}}
            )

        # 2 — Attach payment method to customer
        stripe.PaymentMethod.attach(
            payment_method_id,
            customer=stripe_customer_id,
        )

        # 3 — Set as default payment method
        stripe.Customer.modify(
            stripe_customer_id,
            invoice_settings={"default_payment_method": payment_method_id},
        )

        # 4 — Create subscription with trial
        sub = stripe.Subscription.create(
            customer          = stripe_customer_id,
            items             = [{"price": PRICE_IDS[plan]}],
            trial_period_days = trial_days,
            payment_settings  = {
                "payment_method_types": ["card"],
                "save_default_payment_method": "on_subscription",
            },
            expand = ["latest_invoice.payment_intent"],
        )

    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e.user_message))

    # 5 — Save subscription to MongoDB
    sub_dict  = sub.to_dict()
    trial_end = datetime.utcfromtimestamp(sub_dict["trial_end"]) if sub_dict.get("trial_end") else None

    # Convert Stripe object to plain dict first
    sub_dict = sub.to_dict()

    await subscriptions_col().insert_one({
        "user_id":                user_id,
        "stripe_customer_id":     stripe_customer_id,
        "stripe_subscription_id": sub_dict.get("id"),
        "plan":                   plan,
        "status":                 sub_dict.get("status"),
        "trial_used":             trial_days > 0,
        "trial_end":              trial_end,
        "current_period_start":   datetime.utcfromtimestamp(sub_dict["current_period_start"]) if sub_dict.get("current_period_start") else None,
        "current_period_end":     datetime.utcfromtimestamp(sub_dict["current_period_end"]) if sub_dict.get("current_period_end") else None,
        "cancel_at_period_end":   False,
        "created_at":             datetime.utcnow(),
        "updated_at":             datetime.utcnow(),
    })

    # 6 — Mark user as premium in users collection
    await users_col().update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "plan":       "premium",
            "updated_at": datetime.utcnow(),
        }}
    )

    return {
        "success":   True,
        "message":   f"7-day free trial started! You won't be charged until {trial_end.strftime('%B %d, %Y') if trial_end else 'trial ends'}.",
        "plan":      plan,
        "status":    sub.status,
        "trial_end": trial_end.isoformat() if trial_end else None,
    }


# ─────────────────────────────────────────────────
#  GET SUBSCRIPTION STATUS
# ─────────────────────────────────────────────────

@router.get("/status")
async def subscription_status(current_user: dict = Depends(_require_auth)):
    """Return current subscription status for the logged-in user."""

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
        "trial_end":            sub["trial_end"].isoformat() if sub.get("trial_end") else None,
        "current_period_end":   sub["current_period_end"].isoformat() if sub.get("current_period_end") else None,
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
    }


# ─────────────────────────────────────────────────
#  CANCEL SUBSCRIPTION
# ─────────────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(
    reason:       Optional[str] = None,
    current_user: dict          = Depends(_require_auth),
):
    sub = await subscriptions_col().find_one({
        "user_id": str(current_user["_id"]),
        "status":  {"$in": ["trialing", "active"]},
    })
    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found.")

    try:
        # ── Modify and convert to plain dict ────
        updated = stripe.Subscription.modify(
            sub["stripe_subscription_id"],
            cancel_at_period_end=True,
        )
        updated_dict = updated.to_dict()

        # Get period end from Stripe response
        period_end_ts  = updated_dict.get("current_period_end") or \
                         updated_dict.get("trial_end")
        period_end_dt  = datetime.utcfromtimestamp(period_end_ts) if period_end_ts else None
        period_end_str = period_end_dt.strftime("%B %d, %Y") if period_end_dt else "your billing period"

    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e.user_message))

    # Update MongoDB
    await subscriptions_col().update_one(
        {"_id": sub["_id"]},
        {"$set": {
            "cancel_at_period_end": True,
            "cancel_reason":        reason,
            "updated_at":           datetime.utcnow(),
        }}
    )

    return {
        "success": True,
        "message": f"Subscription cancelled. You'll have premium access until {period_end_str}.",
    }

# ─────────────────────────────────────────────────
#  STRIPE WEBHOOK
#  Handles: trial ended → charge, payment failed,
#           subscription cancelled
# ─────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None),
):
    import json
    payload = await request.body()

    # Verify signature
    try:
        stripe.Webhook.construct_event(
            payload, stripe_signature, WEBHOOK_SECRET
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    # Parse as plain JSON dict — avoids Stripe object issues
    event_dict = json.loads(payload)
    data       = event_dict["data"]["object"]
    etype      = event_dict["type"]

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data.get("id")
        status = data.get("status")

        update = {
            "status":     status,
            "updated_at": datetime.utcnow(),
        }

        if data.get("current_period_end"):
            update["current_period_end"] = datetime.utcfromtimestamp(
                data["current_period_end"]
            )

        await subscriptions_col().update_one(
            {"stripe_subscription_id": sub_id},
            {"$set": update}
        )

        if status in ("canceled", "unpaid", "past_due"):
            sub_doc = await subscriptions_col().find_one(
                {"stripe_subscription_id": sub_id}
            )
            if sub_doc:
                await users_col().update_one(
                    {"_id": ObjectId(sub_doc["user_id"])},
                    {"$set": {"plan": "free", "updated_at": datetime.utcnow()}}
                )

        if status == "active":
            sub_doc = await subscriptions_col().find_one(
                {"stripe_subscription_id": sub_id}
            )
            if sub_doc:
                await users_col().update_one(
                    {"_id": ObjectId(sub_doc["user_id"])},
                    {"$set": {"plan": "premium", "updated_at": datetime.utcnow()}}
                )

    elif etype == "invoice.payment_succeeded":
        print(f"✅ Payment succeeded for customer: {data.get('customer')}")

    elif etype == "invoice.payment_failed":
        print(f"❌ Payment failed for customer: {data.get('customer')}")

    return {"success": True}