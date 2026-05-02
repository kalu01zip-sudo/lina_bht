# routers/admin_subscription.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Admin Subscription & Revenue                ║
║                                                                  ║
║  All endpoints require a valid admin JWT                        ║
║  (Authorization: Bearer <admin_access_token>)                   ║
║                                                                  ║
║  Endpoints:                                                      ║
║   GET   /admin/subscription/overview          → MRR, active     ║
║                                                 subs, churn     ║
║   GET   /admin/subscription/plans             → both plan       ║
║                                                 configs         ║
║   PATCH /admin/subscription/plans/basic       → edit free plan  ║
║                                                 limits          ║
║   PATCH /admin/subscription/plans/premium     → edit premium    ║
║                                                 pricing &       ║
║                                                 features        ║
║                                                                  ║
║  Plan config is stored in the 'plan_config' MongoDB collection. ║
║  On first GET /plans call the collection is seeded from the     ║
║  PLANS dict in subscription.py (single source of truth).        ║
║                                                                  ║
║  Revenue calculations:                                          ║
║   • MRR  = active_monthly × price + active_yearly × (price/12) ║
║   • Churn = expired/cancelled this month ÷ start-of-month base ║
║   • % changes compare current period vs 30-day prior window.   ║
║   • For exact store-settled figures use the RevenueCat          ║
║     dashboard (URL returned in the overview response).          ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.database import get_db, subscriptions_col
from app.routers.admin_auth import _get_current_admin

logger = logging.getLogger(__name__)

router       = APIRouter(prefix="/admin/subscription", tags=["Admin Subscription"])
CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]

# ── RevenueCat config ─────────────────────────────────────────────────────────

RC_PROJECT_ID = os.environ.get("REVENUECAT_PROJECT_ID", "")
_RC_DASHBOARD = (
    f"https://app.revenuecat.com/projects/{RC_PROJECT_ID}/charts"
    if RC_PROJECT_ID else
    "https://app.revenuecat.com"
)

# Default plan values — mirrors subscription.py PLANS dict.
# These seed the plan_config collection on first launch.
_DEFAULT_BASIC = {
    "_id":                "basic",
    "plan_type":          "basic",
    "display_name":       "Basic (Free)",
    "price_monthly":      0.0,
    "scans_per_month":    3,
    "product_analysis":   "Basic",
    "ai_coaching":        "None",
}

_DEFAULT_PREMIUM = {
    "_id":                "premium",
    "plan_type":          "premium",
    "display_name":       "SkinSense Premium",
    "price_monthly":      4.99,
    "price_yearly":       49.99,
    "trial_days":         7,
    "scans_per_month":    "Unlimited",
    "product_analysis":   "Full Compatibility",
    "ai_coaching":        "Unlimited",
}


# ── DB shortcuts ──────────────────────────────────────────────────────────────

def _plan_col():
    return get_db()["plan_config"]

def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, matches MongoDB


# ── Plan config seeder ────────────────────────────────────────────────────────

async def _ensure_plan_config():
    """
    Seeds the plan_config collection from defaults if documents are missing.
    Safe to call on every request — uses upsert with $setOnInsert so existing
    admin-edited values are never overwritten.
    """
    now = _utc_now()
    col = _plan_col()
    for defaults in (_DEFAULT_BASIC, _DEFAULT_PREMIUM):
        doc_id = defaults["_id"]
        seed   = {k: v for k, v in defaults.items() if k != "_id"}
        seed["updated_at"] = now
        await col.update_one(
            {"_id": doc_id},
            {"$setOnInsert": seed},
            upsert=True,
        )


# ── Schemas ───────────────────────────────────────────────────────────────────

class StatCard(BaseModel):
    value:       float
    change_pct:  Optional[float]   # positive = up, negative = down, null = no prior data
    direction:   str               # "up" | "down" | "neutral"


class OverviewResponse(BaseModel):
    mrr:                StatCard
    active_subscribers: StatCard
    churn_rate:         StatCard   # value is a percentage, e.g. 2.4 means 2.4 %
    rc_dashboard_url:   str
    generated_at:       str
    revenue_note:       str


class BasicPlanResponse(BaseModel):
    plan_type:          str        # always "basic"
    display_name:       str
    price_monthly:      float      # always 0.0
    user_count:         int
    scans_per_month:    Union[int, str]   # int or "Unlimited"
    product_analysis:   str
    ai_coaching:        str
    updated_at:         str


class PremiumPlanResponse(BaseModel):
    plan_type:          str        # always "premium"
    display_name:       str
    price_monthly:      float
    price_yearly:       float
    trial_days:         int
    user_count:         int
    scans_per_month:    Union[int, str]
    product_analysis:   str
    ai_coaching:        str
    updated_at:         str


class PlansResponse(BaseModel):
    basic:   BasicPlanResponse
    premium: PremiumPlanResponse


class EditBasicPlanRequest(BaseModel):
    scans_per_month:  Optional[Union[int, str]] = Field(
        None,
        description="Number of scans allowed per month, or 'Unlimited'",
        examples=[3, "Unlimited"],
    )
    product_analysis: Optional[str] = Field(
        None,
        description="e.g. 'Basic', 'Full Compatibility', 'None'",
    )
    ai_coaching:      Optional[str] = Field(
        None,
        description="e.g. 'None', 'Unlimited', 'Basic'",
    )

    model_config = {"json_schema_extra": {"example": {
        "scans_per_month":  5,
        "product_analysis": "Basic",
        "ai_coaching":      "None",
    }}}


class EditPremiumPlanRequest(BaseModel):
    display_name:     Optional[str]   = None
    price_monthly:    Optional[float] = Field(None, gt=0)
    price_yearly:     Optional[float] = Field(None, gt=0)
    trial_days:       Optional[int]   = Field(None, ge=0, le=365)
    scans_per_month:  Optional[Union[int, str]] = Field(
        None,
        description="int or 'Unlimited'",
    )
    product_analysis: Optional[str]   = None
    ai_coaching:      Optional[str]   = None

    model_config = {"json_schema_extra": {"example": {
        "price_monthly":    4.99,
        "price_yearly":     49.99,
        "scans_per_month":  "Unlimited",
        "product_analysis": "Full Compatibility",
        "ai_coaching":      "Unlimited",
    }}}


# ── Revenue calculation helpers ───────────────────────────────────────────────

def _direction(pct: Optional[float]) -> str:
    if pct is None or abs(pct) < 0.01:
        return "neutral"
    return "up" if pct > 0 else "down"


def _pct_change(current: float, prior: float) -> Optional[float]:
    if prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


async def _count_active(before: Optional[datetime] = None) -> dict:
    """
    Returns {"monthly": int, "yearly": int} for the active subscriber counts.

    If `before` is given, counts subscriptions that WERE active at that point in time:
      • created before `before`
      • expires_at is None  OR  expires_at > before
    This approximates the subscriber base at a past snapshot.
    """
    subs_col = subscriptions_col()

    if before is None:
        # Current active
        pipeline = [
            {"$match": {"status": {"$in": ["active", "trialing"]}}},
            {"$group": {
                "_id":   "$plan_type",
                "count": {"$sum": 1},
            }},
        ]
    else:
        # Past snapshot — created before `before` and not yet expired
        pipeline = [
            {"$match": {
                "created_at": {"$lte": before},
                "$or": [
                    {"expires_at": None},
                    {"expires_at": {"$exists": False}},
                    {"expires_at": {"$gt": before}},
                ],
            }},
            {"$group": {
                "_id":   "$plan_type",
                "count": {"$sum": 1},
            }},
        ]

    rows = await subs_col.aggregate(pipeline).to_list(None)
    result = {"monthly": 0, "yearly": 0}
    for row in rows:
        pt = row.get("_id") or "monthly"
        if pt in result:
            result[pt] = row["count"]
    return result


async def _count_churned(since: datetime, until: Optional[datetime] = None) -> int:
    """
    Subscriptions whose status changed to expired/cancelled between `since` and `until`.
    `until` defaults to now.
    """
    time_filter: dict = {"$gte": since}
    if until:
        time_filter["$lt"] = until

    return await subscriptions_col().count_documents({
        "status":     {"$in": ["expired", "cancelled"]},
        "updated_at": time_filter,
    })


def _calc_mrr(counts: dict, monthly_price: float, yearly_price: float) -> float:
    return round(
        counts["monthly"] * monthly_price +
        counts["yearly"]  * (yearly_price / 12),
        2,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/overview",
    response_model = OverviewResponse,
    summary        = "Subscription & Revenue overview — top stats",
    description    = (
        "Returns the three KPI cards shown at the top of the "
        "Subscription & Revenue admin page.\n\n"
        "| Field | What it shows |\n"
        "|-------|---------------|\n"
        "| `mrr` | Monthly Recurring Revenue (USD) |\n"
        "| `active_subscribers` | Users on active or trialing plan |\n"
        "| `churn_rate` | % of subscribers who churned this calendar month |\n\n"
        "Each card includes `change_pct` (vs 30-day prior window) "
        "and `direction` (`up` / `down` / `neutral`) for the trend arrow.\n\n"
        "**Revenue note:** MRR is estimated from MongoDB subscription records "
        "using the plan prices stored in `plan_config`. "
        "For store-settled payout figures, follow `rc_dashboard_url`."
    ),
)
async def subscription_overview(current_admin: CurrentAdmin):
    await _ensure_plan_config()

    now              = _utc_now()
    thirty_days_ago  = now - timedelta(days=30)
    sixty_days_ago   = now - timedelta(days=60)
    start_of_month   = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Last month's start (for churn comparison)
    if start_of_month.month == 1:
        start_of_last_month = start_of_month.replace(year=start_of_month.year - 1, month=12)
    else:
        start_of_last_month = start_of_month.replace(month=start_of_month.month - 1)

    # ── Fetch premium plan prices from plan_config ────────────────────────────
    premium_cfg   = await _plan_col().find_one({"_id": "premium"}) or _DEFAULT_PREMIUM
    monthly_price = float(premium_cfg.get("price_monthly", 4.99))
    yearly_price  = float(premium_cfg.get("price_yearly",  49.99))

    # ── Active subscriber counts ──────────────────────────────────────────────
    current_counts      = await _count_active()
    prior_counts        = await _count_active(before=thirty_days_ago)

    current_active_total = current_counts["monthly"] + current_counts["yearly"]
    prior_active_total   = prior_counts["monthly"]   + prior_counts["yearly"]

    # ── MRR ───────────────────────────────────────────────────────────────────
    current_mrr = _calc_mrr(current_counts, monthly_price, yearly_price)
    prior_mrr   = _calc_mrr(prior_counts,   monthly_price, yearly_price)

    mrr_change    = _pct_change(current_mrr,          prior_mrr)
    active_change = _pct_change(current_active_total, prior_active_total)

    # ── Churn rate (this calendar month) ─────────────────────────────────────
    # Base = subscriptions active at start of this month (approximated)
    start_of_month_counts  = await _count_active(before=start_of_month)
    start_of_month_total   = (
        start_of_month_counts["monthly"] + start_of_month_counts["yearly"]
    )
    churned_this_month      = await _count_churned(since=start_of_month)
    current_churn_rate      = round(
        churned_this_month / start_of_month_total * 100, 1
    ) if start_of_month_total > 0 else 0.0

    # Prior month churn rate
    start_of_lm_counts  = await _count_active(before=start_of_last_month)
    start_of_lm_total   = start_of_lm_counts["monthly"] + start_of_lm_counts["yearly"]
    churned_last_month  = await _count_churned(
        since=start_of_last_month, until=start_of_month
    )
    prior_churn_rate    = round(
        churned_last_month / start_of_lm_total * 100, 1
    ) if start_of_lm_total > 0 else 0.0

    churn_change = _pct_change(current_churn_rate, prior_churn_rate)

    # For churn, "up" is bad — but direction is still numerically "up"
    # Frontend can invert the colour based on the metric type.

    return OverviewResponse(
        mrr = StatCard(
            value      = current_mrr,
            change_pct = mrr_change,
            direction  = _direction(mrr_change),
        ),
        active_subscribers = StatCard(
            value      = float(current_active_total),
            change_pct = active_change,
            direction  = _direction(active_change),
        ),
        churn_rate = StatCard(
            value      = current_churn_rate,
            change_pct = churn_change,
            direction  = _direction(churn_change),
        ),
        rc_dashboard_url = _RC_DASHBOARD,
        generated_at     = now.isoformat() + "Z",
        revenue_note = (
            "MRR is estimated from MongoDB subscription records using plan prices "
            f"(${monthly_price:.2f}/mo · ${yearly_price:.2f}/yr). "
            "Churn = expired/cancelled this calendar month ÷ active at month start. "
            "For store-settled payout figures, visit the RevenueCat dashboard."
        ),
    )


@router.get(
    "/plans",
    response_model = PlansResponse,
    summary        = "Get both plan configs (Basic + Premium)",
    description    = (
        "Returns the full config for both pricing plans as shown on "
        "the Subscription & Revenue page.\n\n"
        "`user_count` reflects current live subscriber counts from the "
        "`subscriptions` collection.\n\n"
        "On first call the `plan_config` collection is automatically seeded "
        "from the defaults in `subscription.py` — no manual setup needed."
    ),
)
async def get_plans(current_admin: CurrentAdmin):
    await _ensure_plan_config()

    basic_cfg   = await _plan_col().find_one({"_id": "basic"})   or _DEFAULT_BASIC
    premium_cfg = await _plan_col().find_one({"_id": "premium"}) or _DEFAULT_PREMIUM

    # Live user counts
    free_count    = await get_db()["users"].count_documents(
        {"plan": {"$in": ["free", None, ""]}}
    )
    active_counts = await _count_active()
    premium_total = active_counts["monthly"] + active_counts["yearly"]

    def _ts(doc: dict) -> str:
        v = doc.get("updated_at")
        return v.isoformat() if isinstance(v, datetime) else str(v or "")

    basic = BasicPlanResponse(
        plan_type        = "basic",
        display_name     = basic_cfg.get("display_name", "Basic (Free)"),
        price_monthly    = float(basic_cfg.get("price_monthly", 0.0)),
        user_count       = free_count,
        scans_per_month  = basic_cfg.get("scans_per_month", 3),
        product_analysis = basic_cfg.get("product_analysis", "Basic"),
        ai_coaching      = basic_cfg.get("ai_coaching", "None"),
        updated_at       = _ts(basic_cfg),
    )

    premium = PremiumPlanResponse(
        plan_type        = "premium",
        display_name     = premium_cfg.get("display_name", "SkinSense Premium"),
        price_monthly    = float(premium_cfg.get("price_monthly", 4.99)),
        price_yearly     = float(premium_cfg.get("price_yearly",  49.99)),
        trial_days       = int(premium_cfg.get("trial_days", 7)),
        user_count       = premium_total,
        scans_per_month  = premium_cfg.get("scans_per_month", "Unlimited"),
        product_analysis = premium_cfg.get("product_analysis", "Full Compatibility"),
        ai_coaching      = premium_cfg.get("ai_coaching", "Unlimited"),
        updated_at       = _ts(premium_cfg),
    )

    return PlansResponse(basic=basic, premium=premium)


@router.patch(
    "/plans/basic",
    response_model = BasicPlanResponse,
    summary        = "Edit Basic (free) plan limits",
    description    = (
        "Updates the feature limits shown on the Basic plan card.\n\n"
        "All fields are optional — only include what you want to change.\n\n"
        "| Field | Examples |\n"
        "|-------|----------|\n"
        "| `scans_per_month` | `3`, `5`, `10`, `\"Unlimited\"` |\n"
        "| `product_analysis` | `\"Basic\"`, `\"Full Compatibility\"`, `\"None\"` |\n"
        "| `ai_coaching` | `\"None\"`, `\"Basic\"`, `\"Unlimited\"` |\n\n"
        "**Note:** changing these values updates the admin dashboard display only. "
        "Enforcement logic in your scan/AI endpoints must read from "
        "`GET /admin/subscription/plans` to respect these limits."
    ),
)
async def edit_basic_plan(
    payload:       EditBasicPlanRequest,
    current_admin: CurrentAdmin,
):
    await _ensure_plan_config()

    updates: dict = {"updated_at": _utc_now()}

    if payload.scans_per_month  is not None: updates["scans_per_month"]  = payload.scans_per_month
    if payload.product_analysis is not None: updates["product_analysis"] = payload.product_analysis.strip()
    if payload.ai_coaching      is not None: updates["ai_coaching"]      = payload.ai_coaching.strip()

    if len(updates) == 1:   # only updated_at → nothing to do
        raise HTTPException(status_code=400, detail="No fields to update.")

    await _plan_col().update_one(
        {"_id": "basic"},
        {"$set": updates},
        upsert=True,
    )

    logger.info(
        "Admin %s updated basic plan: %s",
        current_admin.get("email"), {k: v for k, v in updates.items() if k != "updated_at"},
    )

    # Return refreshed plan (with live user count)
    return (await get_plans(current_admin)).basic


@router.patch(
    "/plans/premium",
    response_model = PremiumPlanResponse,
    summary        = "Edit Premium plan pricing & features",
    description    = (
        "Updates the Premium plan config — pricing, trial length, and feature labels.\n\n"
        "All fields are optional — only include what you want to change.\n\n"
        "| Field | Notes |\n"
        "|-------|-------|\n"
        "| `price_monthly` | Monthly billing price (USD, must be > 0) |\n"
        "| `price_yearly` | Yearly billing price (USD, must be > 0) |\n"
        "| `trial_days` | Free trial length in days (0 = no trial) |\n"
        "| `scans_per_month` | int or `\"Unlimited\"` |\n"
        "| `product_analysis` | e.g. `\"Full Compatibility\"` |\n"
        "| `ai_coaching` | e.g. `\"Unlimited\"` |\n\n"
        "**Important:** price changes here update the **dashboard display** only. "
        "To change actual billing prices you must update your RevenueCat product "
        "configuration in the App Store / Google Play Console."
    ),
)
async def edit_premium_plan(
    payload:       EditPremiumPlanRequest,
    current_admin: CurrentAdmin,
):
    await _ensure_plan_config()

    updates: dict = {"updated_at": _utc_now()}

    if payload.display_name     is not None: updates["display_name"]     = payload.display_name.strip()
    if payload.price_monthly    is not None: updates["price_monthly"]    = payload.price_monthly
    if payload.price_yearly     is not None: updates["price_yearly"]     = payload.price_yearly
    if payload.trial_days       is not None: updates["trial_days"]       = payload.trial_days
    if payload.scans_per_month  is not None: updates["scans_per_month"]  = payload.scans_per_month
    if payload.product_analysis is not None: updates["product_analysis"] = payload.product_analysis.strip()
    if payload.ai_coaching      is not None: updates["ai_coaching"]      = payload.ai_coaching.strip()

    if len(updates) == 1:
        raise HTTPException(status_code=400, detail="No fields to update.")

    await _plan_col().update_one(
        {"_id": "premium"},
        {"$set": updates},
        upsert=True,
    )

    logger.info(
        "Admin %s updated premium plan: %s",
        current_admin.get("email"), {k: v for k, v in updates.items() if k != "updated_at"},
    )

    return (await get_plans(current_admin)).premium
