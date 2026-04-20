# routers/subscription.py
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  SkinSense — Subscription Module (RevenueCat v2)                        ║
║                                                                          ║
║  Plans:                                                                  ║
║    • Monthly  — $4.99/mo   + 7-day free trial                           ║
║    • Yearly   — $49.99/yr  + 7-day free trial  (~17% savings)           ║
║                                                                          ║
║  Endpoints:                                                              ║
║    GET  /subscription/plans          → paywall plan info (no auth)      ║
║    POST /subscription/verify         → mobile calls after SDK purchase  ║
║    POST /subscription/grant          → admin: force-grant premium       ║
║    GET  /subscription/status         → current plan + trial status      ║
║    POST /subscription/cancel         → returns store cancel deep-link   ║
║    POST /subscription/webhook        → RevenueCat lifecycle events      ║
║    GET  /subscription/debug-rc/{id}  → DEV ONLY, remove in prod        ║
║                                                                          ║
║  RevenueCat Dashboard Setup (one-time):                                  ║
║    1. Products                                                           ║
║       • skinsense_premium_monthly   ($4.99,  7-day trial)               ║
║       • skinsense_premium_yearly    ($49.99, 7-day trial)               ║
║    2. Entitlements                                                       ║
║       • "GIXY Premium"  ← attach BOTH products to this one entitlement  ║
║    3. Offerings → default                                                ║
║       • Package: Monthly → skinsense_premium_monthly                    ║
║       • Package: Yearly  → skinsense_premium_yearly                     ║
║    4. Webhooks → Add endpoint                                            ║
║       URL:  https://yourserver.com/subscription/webhook                 ║
║       Auth: Bearer <REVENUECAT_WEBHOOK_AUTH_TOKEN>                      ║
║                                                                          ║
║  Mobile SDK setup (React Native) — do this once at app start:           ║
║    import Purchases from 'react-native-purchases';                       ║
║    Purchases.configure({ apiKey: RC_PUBLIC_KEY });                       ║
║    await Purchases.logIn(mongoUserId);  ← critical: links RC to user    ║
║                                                                          ║
║  Full automated lifecycle (zero admin needed):                           ║
║    Purchase → INITIAL_PURCHASE(trial) → RENEWAL(charged)                ║
║    → RENEWAL(cycle) → CANCELLATION → EXPIRATION(free)                   ║
║                                                                          ║
║  .env keys required:                                                     ║
║    REVENUECAT_V2_API_KEY           RC Dashboard → API Keys → Secret     ║
║    REVENUECAT_PROJECT_ID           from RC Dashboard URL                 ║
║    REVENUECAT_WEBHOOK_AUTH_TOKEN   set in RC Dashboard → Webhooks        ║
║    RC_ENTITLEMENT_ID               e.g. "GIXY Premium"                  ║
║    ADMIN_SECRET                    protects /grant endpoint              ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import logging
import httpx

from datetime import datetime
from typing   import Optional, Annotated
from fastapi  import APIRouter, Depends, HTTPException, Request, Header
from bson     import ObjectId
from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from database   import users_col, subscriptions_col
from auth_utils import decode_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subscription", tags=["Subscription"])
bearer = HTTPBearer()


# ── Config ───────────────────────────────────────────────────────────────────

RC_V2_API_KEY         = os.environ.get("REVENUECAT_V2_API_KEY", "")
RC_PROJECT_ID         = os.environ.get("REVENUECAT_PROJECT_ID", "")
RC_WEBHOOK_AUTH_TOKEN = os.environ.get("REVENUECAT_WEBHOOK_AUTH_TOKEN", "")
RC_ENTITLEMENT_ID     = os.environ.get("RC_ENTITLEMENT_ID", "")

RC_V2_BASE = "https://api.revenuecat.com/v2"

# Plan catalog — update prices here; /plans endpoint reads from this dict
PLANS = {
    "monthly": {
        "id":                 "monthly",
        "price":              4.99,
        "currency":           "USD",
        "interval":           "month",
        "trial_days":         7,
        "label":              "Monthly",
        "description":        "Full access, billed every month.",
        "savings_pct":        None,
        "monthly_equivalent": 4.99,
    },
    "yearly": {
        "id":                 "yearly",
        "price":              49.99,
        "currency":           "USD",
        "interval":           "year",
        "trial_days":         7,
        "label":              "Yearly",
        "description":        "Full access, billed once a year.",
        "savings_pct":        17,            # vs monthly x12 = $59.88
        "monthly_equivalent": round(49.99 / 12, 2),
    },
}


# ── Schemas ──────────────────────────────────────────────────────────────────

class VerifyRequest(BaseModel):
    """
    Mobile sends this right after the RC SDK reports a successful purchase.
    app_user_id must equal the MongoDB _id you passed to Purchases.logIn().
    """
    app_user_id: str


class GrantRequest(BaseModel):
    """
    Admin: pass user_id + entitlement_id. Backend checks RC and upgrades user.
    entitlement_id is optional — defaults to RC_ENTITLEMENT_ID from .env.
    """
    user_id:        str
    entitlement_id: Optional[str] = None


# ── Auth helpers ─────────────────────────────────────────────────────────────

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


def _require_admin(x_admin_secret: Optional[str] = Header(None)) -> None:
    """Pass X-Admin-Secret header equal to ADMIN_SECRET from .env."""
    if ADMIN_SECRET and x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")


# ── RevenueCat helpers ───────────────────────────────────────────────────────

def _rc_headers() -> dict:
    return {
        "Authorization": f"Bearer {RC_V2_API_KEY}",
        "Content-Type":  "application/json",
    }


async def _fetch_active_entitlements(app_user_id: str) -> list:
    """
    Calls RC v2 → returns currently active entitlement objects for this user.

    Item shape:
      { "entitlement_id": "entlXXX",  ← RC internal object ID (not display name)
        "expires_at": <ms> | null,    ← null = lifetime / no expiry
        "object": "customer.active_entitlement" }

    RC only returns ACTIVE ones — no need to filter expiry on our side.
    """
    url = (
        f"{RC_V2_BASE}/projects/{RC_PROJECT_ID}"
        f"/customers/{app_user_id}/active_entitlements"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_rc_headers())

    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"RevenueCat API error {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json().get("items", [])


def _ms_to_dt(ms) -> Optional[datetime]:
    if not ms:
        return None
    return datetime.utcfromtimestamp(int(ms) / 1000)


def _days_remaining(expires_at: Optional[datetime]) -> Optional[int]:
    if not expires_at:
        return None
    return max(0, (expires_at - datetime.utcnow()).days)


def _detect_plan_type(product_id: str) -> str:
    """
    Derives 'monthly' or 'yearly' from the RC product_id string.
    Works as long as your product IDs contain 'monthly'/'yearly'/'annual'.
    e.g. 'skinsense_premium_yearly' → 'yearly'
    """
    pid = product_id.lower()
    if any(k in pid for k in ("yearly", "annual", "year")):
        return "yearly"
    return "monthly"


def _event_has_our_entitlement(event_entitlement_ids: list) -> bool:
    """
    Checks if the webhook event targets our configured entitlement.

    In RC webhook payloads, entitlement_ids contains IDENTIFIER strings
    (e.g. "GIXY Premium") — NOT internal RC object IDs.

    Empty list = old RC webhook format → allow through so nothing breaks.
    """
    if not event_entitlement_ids:
        return True
    configured = RC_ENTITLEMENT_ID.lower().strip()
    return any(e.lower().strip() == configured for e in event_entitlement_ids)


# ── Core DB helpers ──────────────────────────────────────────────────────────

async def _upsert_premium(
    *,
    user_id:              str,
    plan_type:            str,      # "monthly" | "yearly"
    status:               str,      # "trialing" | "active" | "past_due"
    is_trial:             bool,
    will_renew:           bool,
    cancel_at_period_end: bool,
    expires_dt:           Optional[datetime],
    rc_entitlement_id:    str = "",
    product_id:           str = "",
    now:                  datetime,
) -> None:
    """
    Upserts the subscription doc and flips user.plan in the users collection.
    Single source of truth — called from /verify, /grant, and the webhook.
    """
    plan_value = "premium" if status in ("active", "trialing") else "free"

    await subscriptions_col().update_one(
        {"user_id": user_id},
        {
            "$set": {
                "plan":                 plan_value,
                "plan_type":            plan_type,
                "status":               status,
                "is_trial":             is_trial,
                "will_renew":           will_renew,
                "cancel_at_period_end": cancel_at_period_end,
                "expires_at":           expires_dt,
                "rc_entitlement_id":    rc_entitlement_id or RC_ENTITLEMENT_ID,
                "product_id":           product_id,
                "updated_at":           now,
            },
            "$setOnInsert": {
                "user_id":        user_id,
                "rc_app_user_id": user_id,
                "created_at":     now,
            },
        },
        upsert=True,
    )
    try:
        await users_col().update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"plan": plan_value, "updated_at": now}},
        )
    except Exception as exc:
        logger.warning("Could not update user.plan for %s: %s", user_id, exc)


async def _downgrade_to_free(user_id: str, now: datetime) -> None:
    """Sets subscription to expired and resets user.plan = 'free'."""
    await subscriptions_col().update_one(
        {"user_id": user_id},
        {"$set": {
            "plan":       "free",
            "status":     "expired",
            "will_renew": False,
            "updated_at": now,
        }}
    )
    try:
        await users_col().update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"plan": "free", "updated_at": now}},
        )
    except Exception as exc:
        logger.warning("Could not downgrade user %s: %s", user_id, exc)

# ─────────────────────────────────────────────────────────────────────────────
#  POST /subscription/verify
#  Mobile calls this right after the RC SDK reports a successful purchase.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/verify")
async def verify_subscription(
    body:         VerifyRequest,
    current_user: dict = Depends(_require_auth),
):
    """
    Verifies the purchase with RC and activates premium in MongoDB.

    Mobile flow:
      1. User taps "Start Free Trial" on paywall
      2. RC SDK handles the purchase with Apple / Google
      3. SDK reports success → mobile calls POST /subscription/verify
      4. Backend confirms with RC API → sets user.plan = "premium"

    Body: { "app_user_id": "<MongoDB _id>" }
    The app_user_id must match the logged-in user's _id (security enforced).
    """
    user_id = str(current_user["_id"])

    if body.app_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="app_user_id does not match the authenticated user."
        )

    entitlements = await _fetch_active_entitlements(user_id)

    if not entitlements:
        raise HTTPException(
            status_code=402,
            detail=(
                "No active entitlement found on RevenueCat. "
                "The purchase may still be processing — wait a moment and retry. "
                "If this persists, contact support."
            )
        )

    ent        = entitlements[0]
    expires_dt = _ms_to_dt(ent.get("expires_at"))
    now        = datetime.utcnow()

    # Preserve existing plan_type if we already have a subscription doc
    existing  = await subscriptions_col().find_one({"user_id": user_id})
    plan_type = existing.get("plan_type", "monthly") if existing else "monthly"

    is_trial             = False
    status               = "active"

    await _upsert_premium(
        user_id              = user_id,
        plan_type            = plan_type,
        status               = status,
        is_trial             = is_trial,     # webhook will correct if it's a trial
        will_renew           = True,
        cancel_at_period_end = False,
        expires_dt           = expires_dt,
        rc_entitlement_id    = ent.get("entitlement_id", RC_ENTITLEMENT_ID),
        now                  = now,
    )

    logger.info("✅ /verify: user %s activated (expires: %s)", user_id, expires_dt)

    return {
        "success":        True,
        "message":  "Subscription verified and activated." if not is_trial else "Trial started.",
        "plan":           "premium",
        "plan_type":      plan_type,
        "status":         status,
        "is_trial":       is_trial,
        "expires_at":     expires_dt.isoformat() if expires_dt else None,
        "days_remaining": _days_remaining(expires_dt),
        "will_renew":     True,
    }

# ─────────────────────────────────────────────────────────────────────────────
#  GET /subscription/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def subscription_status(current_user: dict = Depends(_require_auth)):
    """
    Returns full subscription state for the authenticated user.

    Use this to:
      • Gate premium features (check plan == "premium")
      • Show trial banner (check is_trial == true)
      • Show expiry countdown (use days_remaining)
      • Drive the "Manage Subscription" screen

    Key response fields:
      plan           → "free" | "premium"
      plan_type      → "monthly" | "yearly" | null
      status         → "active" | "trialing" | "past_due" | "expired" | "inactive"
      is_trial       → true during the 7-day trial window
      days_remaining → days until expiry (null = no expiry / lifetime)
      will_renew     → false = they cancelled but still have access until expiry
      cancel_at_period_end → true if user pressed cancel in store
      plan_info      → full plan object from PLANS dict (price, interval, etc.)
    """
    sub = await subscriptions_col().find_one(
        {"user_id": str(current_user["_id"])},
        sort=[("created_at", -1)],
    )

    if not sub:
        return {
            "success":              True,
            "plan":                 "free",
            "plan_type":            None,
            "status":               "inactive",
            "is_trial":             False,
            "expires_at":           None,
            "days_remaining":       None,
            "will_renew":           False,
            "cancel_at_period_end": False,
            "plan_info":            None,
        }

    expires_at = sub.get("expires_at")
    plan_type  = sub.get("plan_type", "monthly")

    return {
        "success":              True,
        "plan":                 sub.get("plan", "free"),
        "plan_type":            plan_type,
        "status":               sub.get("status"),
        "is_trial":             sub.get("is_trial", False),
        "expires_at":           expires_at.isoformat() if expires_at else None,
        "days_remaining":       _days_remaining(expires_at),
        "will_renew":           sub.get("will_renew", False),
        "cancel_at_period_end": sub.get("cancel_at_period_end", False),
        "plan_info":            PLANS.get(plan_type),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  POST /subscription/webhook
#
#  RevenueCat fires this for every subscription lifecycle event.
#  Register in: RC Dashboard → Project → Integrations → Webhooks
#  Auth header: Bearer <REVENUECAT_WEBHOOK_AUTH_TOKEN>
#
#  Full automated lifecycle (monthly $4.99 + yearly $49.99, both 7-day trial):
#
#  Day 0   → User picks plan → SDK purchase → INITIAL_PURCHASE (TRIAL)
#              status: trialing, is_trial: true
#  Day 7   → Trial ends, not cancelled → RENEWAL (NORMAL)
#              status: active, is_trial: false — user is charged for first time
#  Cycle   → RENEWAL fires each month (or year) → stays active + expiry extended
#  Cancel  → CANCELLATION → cancel_at_period_end: true, will_renew: false
#              (access continues until period ends)
#  Expiry  → EXPIRATION → plan: free
#
#  Cancel during trial:
#    CANCELLATION fires immediately → EXPIRATION fires at trial end → free
#
#  Payment failure:
#    BILLING_ISSUE → status: past_due (RC retries ~2 weeks)
#    Retry success → RENEWAL → active again
#    RC gives up   → EXPIRATION → free
#
#  Plan switch:
#    PRODUCT_CHANGE → plan_type updated (monthly ↔ yearly), RC handles proration
#
#  Re-subscribe after cancelling:
#    UNCANCELLATION → will_renew: true, cancel_at_period_end: false
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def revenuecat_webhook(
    request:       Request,
    authorization: Optional[str] = Header(None),
):
    """Handles all RevenueCat subscription lifecycle events automatically."""

    # ── 1. Verify shared secret ───────────────────────────────────────────────
    if RC_WEBHOOK_AUTH_TOKEN and authorization != RC_WEBHOOK_AUTH_TOKEN:
        logger.warning("❌ Webhook: invalid authorization header")
        raise HTTPException(status_code=401, detail="Invalid webhook authorization.")

    body  = await request.json()
    event = body.get("event", {})
    etype = event.get("type", "")

    # RC sends test events with no user — always return 200 (RC marks it delivered)
    app_user_id = event.get("app_user_id", "")
    if not app_user_id:
        return {"success": True}

    now = datetime.utcnow()

    # ── 2. Parse event fields ─────────────────────────────────────────────────
    expiration_dt         = _ms_to_dt(event.get("expiration_at_ms"))
    product_id            = event.get("product_id", "")
    period_type           = event.get("period_type", "NORMAL")  # TRIAL | NORMAL | INTRO
    event_entitlement_ids = event.get("entitlement_ids", [])    # identifier strings
    is_trial              = period_type == "TRIAL"
    plan_type             = _detect_plan_type(product_id)

    logger.info(
        "📨 RC Webhook | %-22s user=%-24s plan=%-8s trial=%-5s entitlements=%s",
        etype, app_user_id, plan_type, is_trial, event_entitlement_ids
    )

    # ── INITIAL_PURCHASE ─────────────────────────────────────────────────────
    # First purchase. With your setup this always starts as a 7-day trial,
    # so period_type will be "TRIAL" and is_trial will be True.
    if etype == "INITIAL_PURCHASE":
        if not _event_has_our_entitlement(event_entitlement_ids):
            logger.info("⏭️  Skipping INITIAL_PURCHASE — not our entitlement")
            return {"success": True}

        await _upsert_premium(
            user_id              = app_user_id,
            plan_type            = plan_type,
            status               = "trialing" if is_trial else "active",
            is_trial             = is_trial,
            will_renew           = True,
            cancel_at_period_end = False,
            expires_dt           = expiration_dt,
            product_id           = product_id,
            now                  = now,
        )
        label = "trial started 🆕" if is_trial else "activated ✅"
        logger.info("✅ INITIAL_PURCHASE: user %s → %s (%s)", app_user_id, label, plan_type)

    # ── RENEWAL ──────────────────────────────────────────────────────────────
    # Fires when:
    #   • Trial converts to paid (period_type: TRIAL → NORMAL, user gets charged)
    #   • Monthly billing cycle renews
    #   • Yearly billing cycle renews
    elif etype == "RENEWAL":
        if not _event_has_our_entitlement(event_entitlement_ids):
            logger.info("⏭️  Skipping RENEWAL — not our entitlement")
            return {"success": True}

        await _upsert_premium(
            user_id              = app_user_id,
            plan_type            = plan_type,
            status               = "active",
            is_trial             = False,   # RENEWAL always = real payment
            will_renew           = True,
            cancel_at_period_end = False,
            expires_dt           = expiration_dt,
            product_id           = product_id,
            now                  = now,
        )
        logger.info("✅ RENEWAL: user %s active (%s, expires: %s)", app_user_id, plan_type, expiration_dt)

    # ── UNCANCELLATION ───────────────────────────────────────────────────────
    # User cancelled but then re-enabled auto-renew before the period ended.
    # Access was never interrupted — just flip will_renew back to True.
    elif etype == "UNCANCELLATION":
        if not _event_has_our_entitlement(event_entitlement_ids):
            return {"success": True}

        await subscriptions_col().update_one(
            {"user_id": app_user_id},
            {"$set": {
                "will_renew":           True,
                "cancel_at_period_end": False,
                "updated_at":           now,
            }}
        )
        logger.info("🔄 UNCANCELLATION: user %s re-enabled auto-renew", app_user_id)

    # ── PRODUCT_CHANGE ───────────────────────────────────────────────────────
    # User switched between monthly ↔ yearly.
    # RC handles proration with Apple / Google — we just update our record.
    elif etype == "PRODUCT_CHANGE":
        new_product_id = event.get("new_product_id", product_id)
        new_plan_type  = _detect_plan_type(new_product_id)

        await subscriptions_col().update_one(
            {"user_id": app_user_id},
            {"$set": {
                "plan_type":  new_plan_type,
                "product_id": new_product_id,
                "expires_at": expiration_dt,
                "updated_at": now,
            }}
        )
        logger.info(
            "🔄 PRODUCT_CHANGE: user %s → %s (product: %s)",
            app_user_id, new_plan_type, new_product_id
        )

    # ── CANCELLATION ─────────────────────────────────────────────────────────
    # User cancelled in the store. They still have access until period ends.
    # EXPIRATION will fire after that and actually downgrade them.
    #
    # If cancelled during trial: EXPIRATION follows almost immediately.
    elif etype == "CANCELLATION":
        await subscriptions_col().update_one(
            {"user_id": app_user_id},
            {"$set": {
                "cancel_at_period_end": True,
                "will_renew":           False,
                "expires_at":           expiration_dt,
                "updated_at":           now,
            }}
        )
        label = "trial" if is_trial else "period"
        logger.info(
            "🚫 CANCELLATION: user %s cancelled during %s — access until %s",
            app_user_id, label, expiration_dt
        )

    # ── EXPIRATION ───────────────────────────────────────────────────────────
    # Subscription is fully done. Downgrade to free.
    # Fires after:
    #   • Trial ends + user cancelled during trial
    #   • Paid period ends after CANCELLATION
    #   • Billing issue not resolved in RC's grace period
    elif etype == "EXPIRATION":
        await _downgrade_to_free(app_user_id, now)
        logger.info("⬇️  EXPIRATION: user %s → free", app_user_id)

    # ── BILLING_ISSUE ────────────────────────────────────────────────────────
    # Payment failed. RC retries automatically for ~2 weeks (configurable).
    # User keeps access during retry period — we just mark status as past_due.
    # If retry succeeds → RENEWAL fires → back to active.
    # If RC gives up → EXPIRATION fires → downgrade to free.
    elif etype == "BILLING_ISSUE":
        await subscriptions_col().update_one(
            {"user_id": app_user_id},
            {"$set": {
                "status":     "past_due",
                "updated_at": now,
            }}
        )
        logger.warning("⚠️  BILLING_ISSUE: user %s — payment failed, RC retrying", app_user_id)

    # ── TRANSFER ─────────────────────────────────────────────────────────────
    # User logged into a different account on a new device and RC transferred
    # the subscription to a new app_user_id. The RENEWAL / INITIAL_PURCHASE
    # event handles the actual state — we just log this for visibility.
    elif etype == "TRANSFER":
        transferred_to = event.get("transferred_to", [])
        logger.info(
            "📲 TRANSFER: subscription moved from %s → %s",
            app_user_id, transferred_to
        )

    else:
        logger.info("ℹ️  Unhandled RC event: %s for user %s", etype, app_user_id)

    return {"success": True}
