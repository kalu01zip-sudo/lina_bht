# routers/admin_home.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Admin Home Dashboard                        ║
║                                                                  ║
║  All endpoints require a valid admin JWT                        ║
║  (Authorization: Bearer <admin_access_token>)                   ║
║                                                                  ║
║  Endpoints:                                                      ║
║   GET  /admin/home/dashboard     → widgets 1-5 + 6 + 8 bundled ║
║   GET  /admin/home/server-health → widget 7 (real-time load)    ║
║                                                                  ║
║  Widget map:                                                     ║
║   1. total_users          → registered user count               ║
║   2. total_scans          → all scan_results count              ║
║   3. total_premium_users  → active + trialing subscriptions     ║
║   4. revenue              → MRR / ARR estimated from MongoDB    ║
║                             (RC revenue note included)          ║
║   5. weekly_activity      → daily signups + scans last 7 days   ║
║   6. new_products         → routine steps added in last 24 h    ║
║   7. server_health        → CPU / memory / disk (psutil)        ║
║   8. subscription_milestones → milestone progress badges        ║
║                                                                  ║
║  Revenue source:                                                 ║
║   Revenue is estimated from MongoDB subscription data using the  ║
║   plan catalogue prices (monthly $4.99 / yearly $49.99).        ║
║   For exact payout figures use the RevenueCat dashboard.        ║
║   Requires: REVENUECAT_V2_API_KEY, REVENUECAT_PROJECT_ID        ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.core.database import get_db, subscriptions_col, users_col
from app.routers.admin_auth import _get_current_admin

logger = logging.getLogger(__name__)

router       = APIRouter(prefix="/admin/home", tags=["Admin Dashboard"])
CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]


# ── Config ────────────────────────────────────────────────────────────────────

RC_V2_API_KEY = os.environ.get("REVENUECAT_V2_API_KEY", "")
RC_PROJECT_ID = os.environ.get("REVENUECAT_PROJECT_ID", "")
RC_V2_BASE    = "https://api.revenuecat.com/v2"

# Must stay in sync with subscription.py PLANS dict
_PLAN_PRICE: dict[str, float] = {
    "monthly": 4.99,
    "yearly":  49.99,
}

# MRR equivalent for yearly plan (spread monthly)
_PLAN_MRR: dict[str, float] = {
    "monthly": 4.99,
    "yearly":  round(49.99 / 12, 4),
}

MILESTONES = [10, 50, 100, 500, 1_000, 5_000, 10_000]

# psutil is optional — server-health degrades gracefully without it
try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


# ── DB shortcuts ──────────────────────────────────────────────────────────────

def _scans_col():
    return get_db()["scan_results"]

def _steps_col():
    return get_db()["routine_steps"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)   # naive UTC, matches MongoDB


def _date_label(dt: datetime) -> str:
    """Return 'Mon', 'Tue', … label for a datetime."""
    return dt.strftime("%a")   # locale-independent 3-letter weekday


def _build_week_slots() -> list[dict]:
    """
    Returns 7 dicts for today-6 … today, each with:
      date_str  → "YYYY-MM-DD"  (used as merge key)
      label     → "Mon" etc.
      signups   → 0
      scans     → 0
      new_subs  → 0
    """
    today = _utc_now().date()
    slots = []
    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        slots.append({
            "date_str": d.isoformat(),
            "label":    d.strftime("%a"),
            "signups":  0,
            "scans":    0,
            "new_subs": 0,
        })
    return slots


def _slot_index(slots: list[dict], date_str: str) -> int | None:
    for i, s in enumerate(slots):
        if s["date_str"] == date_str:
            return i
    return None


# ── Widget helpers ─────────────────────────────────────────────────────────────

async def _widget_user_count() -> int:
    return await users_col().count_documents({})


async def _widget_scan_count() -> int:
    return await _scans_col().count_documents({})


async def _widget_premium_count() -> dict:
    """
    Returns:
      total     → active + trialing
      active    → status == "active" (paid, not trial)
      trialing  → status == "trialing"
      monthly   → active monthly paid
      yearly    → active yearly paid
      past_due  → billing failed, RC retrying
    """
    pipeline = [
        {"$match": {"status": {"$in": ["active", "trialing", "past_due"]}}},
        {"$group": {
            "_id": {
                "status":    "$status",
                "plan_type": "$plan_type",
            },
            "count": {"$sum": 1},
        }},
    ]
    rows = await subscriptions_col().aggregate(pipeline).to_list(None)

    counts: dict[str, int] = {
        "total":    0,
        "active":   0,
        "trialing": 0,
        "past_due": 0,
        "monthly":  0,
        "yearly":   0,
    }
    for row in rows:
        status    = row["_id"]["status"]
        plan_type = row["_id"].get("plan_type", "monthly")
        n         = row["count"]

        if status in ("active", "trialing"):
            counts["total"]  += n
        if status == "active":
            counts["active"] += n
            if plan_type in counts:
                counts[plan_type] += n
        elif status == "trialing":
            counts["trialing"] += n
        elif status == "past_due":
            counts["past_due"] += n

    return counts


async def _widget_revenue(premium: dict) -> dict:
    """
    Estimates MRR and ARR from active (non-trial) subscriptions in MongoDB.

    Why local estimation?
      • MongoDB has real-time subscription state.
      • RevenueCat's revenue API reflects actual payments processed through
        the App Store / Google Play — for exact payout data use the RC dashboard.
      • This estimate is based on $4.99/mo and $49.99/yr catalogue prices.

    Returns:
      estimated_mrr       → monthly recurring revenue (USD)
      estimated_arr       → annual recurring revenue  (USD)
      active_monthly      → paid monthly subscribers (not trials)
      active_yearly       → paid yearly  subscribers (not trials)
      trialing            → trial users (not yet charged)
      revenue_note        → disclaimer string
      rc_dashboard_url    → link to RC dashboard for exact figures
    """
    monthly_count = premium["monthly"]
    yearly_count  = premium["yearly"]

    mrr = round(
        monthly_count * _PLAN_MRR["monthly"] +
        yearly_count  * _PLAN_MRR["yearly"],
        2,
    )
    arr = round(mrr * 12, 2)

    rc_url = (
        f"https://app.revenuecat.com/projects/{RC_PROJECT_ID}/charts"
        if RC_PROJECT_ID else
        "https://app.revenuecat.com"
    )

    return {
        "estimated_mrr":    mrr,
        "estimated_arr":    arr,
        "active_monthly":   monthly_count,
        "active_yearly":    yearly_count,
        "trialing":         premium["trialing"],
        "revenue_note": (
            "MRR/ARR estimated from MongoDB subscription records "
            "using catalogue prices ($4.99/mo · $49.99/yr). "
            "Trial users are not included. "
            "For exact payout figures, see the RevenueCat dashboard."
        ),
        "rc_dashboard_url": rc_url,
    }


async def _widget_weekly_activity() -> list[dict]:
    """
    Returns one entry per day for the past 7 days (including today):
      date_str, label, signups, scans, new_subs
    """
    now   = _utc_now()
    start = datetime.combine(
        (now - timedelta(days=6)).date(),
        datetime.min.time(),
    )

    slots = _build_week_slots()

    # ── Daily signups ─────────────────────────────────────────────────────────
    signup_pipeline = [
        {"$match": {"created_at": {"$gte": start}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1},
        }},
    ]
    for row in await users_col().aggregate(signup_pipeline).to_list(None):
        idx = _slot_index(slots, row["_id"])
        if idx is not None:
            slots[idx]["signups"] = row["count"]

    # ── Daily scans ───────────────────────────────────────────────────────────
    scan_pipeline = [
        {"$match": {"scanned_at": {"$gte": start}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$scanned_at"}},
            "count": {"$sum": 1},
        }},
    ]
    for row in await _scans_col().aggregate(scan_pipeline).to_list(None):
        idx = _slot_index(slots, row["_id"])
        if idx is not None:
            slots[idx]["scans"] = row["count"]

    # ── Daily new subscriptions ───────────────────────────────────────────────
    sub_pipeline = [
        {"$match": {
            "created_at": {"$gte": start},
            "status":     {"$in": ["active", "trialing"]},
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1},
        }},
    ]
    for row in await subscriptions_col().aggregate(sub_pipeline).to_list(None):
        idx = _slot_index(slots, row["_id"])
        if idx is not None:
            slots[idx]["new_subs"] = row["count"]

    # Strip internal key before returning
    for s in slots:
        del s["date_str"]

    return slots


async def _widget_new_products(window_hours: int = 24) -> dict:
    """
    Returns routine steps added in the last `window_hours` hours.
    Grouped by product_name with add count so the admin can see trending
    product additions across all users.

    Response shape:
      window_hours          → look-back window used
      total_added           → total routine steps added
      top_products          → top-10 products by add count
        - product_name
        - time_slot         → morning | night | weekly
        - added_count       → how many users added this step
      recent_steps          → last 20 individual additions (newest first)
        - product_name, time_slot, user_id, created_at
    """
    since = _utc_now() - timedelta(hours=window_hours)

    # Total count
    total = await _steps_col().count_documents({"created_at": {"$gte": since}})

    # Top products
    top_pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {
            "_id": {
                "product_name": "$product_name",
                "time_slot":    "$time_slot",
            },
            "added_count": {"$sum": 1},
        }},
        {"$sort": {"added_count": -1}},
        {"$limit": 10},
    ]
    top_raw = await _steps_col().aggregate(top_pipeline).to_list(None)
    top_products = [
        {
            "product_name": row["_id"]["product_name"],
            "time_slot":    row["_id"]["time_slot"],
            "added_count":  row["added_count"],
        }
        for row in top_raw
    ]

    # Recent 20 individual additions
    recent_docs = await _steps_col().find(
        {"created_at": {"$gte": since}},
        {"product_name": 1, "time_slot": 1, "user_id": 1, "created_at": 1},
    ).sort("created_at", -1).limit(20).to_list(None)

    recent_steps = [
        {
            "product_name": d["product_name"],
            "time_slot":    d.get("time_slot"),
            "user_id":      d.get("user_id"),
            "created_at":   d["created_at"].isoformat() if d.get("created_at") else None,
        }
        for d in recent_docs
    ]

    return {
        "window_hours": window_hours,
        "total_added":  total,
        "top_products": top_products,
        "recent_steps": recent_steps,
    }


def _widget_server_health() -> dict:
    """
    Returns CPU %, memory %, and disk % using psutil.
    If psutil is not installed, returns an instructional error.

    Thresholds used for status labels:
      green  → < 60 %
      yellow → 60–80 %
      red    → > 80 %
    """
    if not _PSUTIL_OK:
        return {
            "available": False,
            "error": (
                "psutil is not installed. "
                "Run `pip install psutil` and restart the server."
            ),
        }

    def _label(pct: float) -> str:
        if pct < 60:   return "green"
        if pct < 80:   return "yellow"
        return "red"

    cpu_pct  = _psutil.cpu_percent(interval=0.5)
    mem      = _psutil.virtual_memory()
    disk     = _psutil.disk_usage("/")

    mem_pct  = mem.percent
    disk_pct = disk.percent

    return {
        "available": True,
        "cpu": {
            "percent": cpu_pct,
            "status":  _label(cpu_pct),
        },
        "memory": {
            "percent":       mem_pct,
            "used_gb":       round(mem.used  / 1024 ** 3, 2),
            "total_gb":      round(mem.total / 1024 ** 3, 2),
            "available_gb":  round(mem.available / 1024 ** 3, 2),
            "status":        _label(mem_pct),
        },
        "disk": {
            "percent":  disk_pct,
            "used_gb":  round(disk.used  / 1024 ** 3, 2),
            "total_gb": round(disk.total / 1024 ** 3, 2),
            "free_gb":  round(disk.free  / 1024 ** 3, 2),
            "status":   _label(disk_pct),
        },
        "overall_status": max(
            [_label(cpu_pct), _label(mem_pct), _label(disk_pct)],
            key=lambda s: {"green": 0, "yellow": 1, "red": 2}[s],
        ),
    }


async def _widget_subscription_milestones(total_premium: int) -> dict:
    """
    Shows which milestones have been reached and what the next one is.

    Returns:
      current_count   → current premium user count
      reached         → list of milestones already passed
      next_milestone  → next milestone to hit (or null if beyond all)
      users_to_next   → how many more users needed for the next milestone
      progress_pct    → progress % toward the next milestone
    """
    reached     = [m for m in MILESTONES if total_premium >= m]
    upcoming    = [m for m in MILESTONES if total_premium < m]
    next_ms     = upcoming[0] if upcoming else None

    if next_ms is not None:
        prev        = reached[-1] if reached else 0
        span        = next_ms - prev
        progress    = total_premium - prev
        pct         = round(progress / span * 100, 1) if span else 100.0
        users_left  = next_ms - total_premium
    else:
        pct        = 100.0
        users_left = 0

    return {
        "current_count":   total_premium,
        "milestones":      MILESTONES,
        "reached":         reached,
        "next_milestone":  next_ms,
        "users_to_next":   users_left,
        "progress_pct":    pct,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/dashboard",
    summary     = "Admin home dashboard — all widgets",
    description = (
        "Returns all 8 dashboard widgets in a single call.\n\n"
        "| Field | Widget |\n"
        "|-------|--------|\n"
        "| `total_users` | 1 — Total registered users |\n"
        "| `total_scans` | 2 — Total scans performed |\n"
        "| `premium` | 3 — Premium user breakdown |\n"
        "| `revenue` | 4 — Estimated MRR / ARR (catalogue prices) |\n"
        "| `weekly_activity` | 5 — Daily signups + scans (last 7 days) |\n"
        "| `new_products` | 6 — Routine products added (last 24 h) |\n"
        "| `subscription_milestones` | 8 — Milestone badge progress |\n\n"
        "Widget 7 (server health) is intentionally excluded here — "
        "poll `GET /admin/home/server-health` separately on a short interval "
        "so its `cpu_percent` blocking call does not slow the main page load.\n\n"
        "**Revenue note:** Figures are estimated from MongoDB subscription "
        "records. For exact store-settled amounts, visit the RevenueCat dashboard "
        "(link included in the `revenue` field)."
    ),
)
async def admin_dashboard(current_admin: CurrentAdmin):
    # ── Gather all widgets concurrently ──────────────────────────────────────
    import asyncio

    (
        total_users,
        total_scans,
        premium,
        weekly_activity,
        new_products,
    ) = await asyncio.gather(
        _widget_user_count(),
        _widget_scan_count(),
        _widget_premium_count(),
        _widget_weekly_activity(),
        _widget_new_products(window_hours=24),
    )

    revenue   = await _widget_revenue(premium)
    milestones = await _widget_subscription_milestones(premium["total"])

    return {
        "success":                True,
        "generated_at":           _utc_now().isoformat() + "Z",

        # Widget 1
        "total_users":            total_users,

        # Widget 2
        "total_scans":            total_scans,

        # Widget 3
        "premium":                premium,

        # Widget 4
        "revenue":                revenue,

        # Widget 5
        "weekly_activity":        weekly_activity,

        # Widget 6
        "new_products":           new_products,

        # Widget 8
        "subscription_milestones": milestones,
    }


@router.get(
    "/server-health",
    summary     = "Server load — CPU / memory / disk",
    description = (
        "**Widget 7 — Real-time server health.**\n\n"
        "Poll this endpoint independently (e.g. every 10 s) to avoid "
        "blocking the main dashboard load.\n\n"
        "Requires `psutil` to be installed (`pip install psutil`).\n\n"
        "Status labels:\n"
        "- `green`  → < 60 %\n"
        "- `yellow` → 60–80 %\n"
        "- `red`    → > 80 %\n\n"
        "The `overall_status` field is the worst status across all three metrics."
    ),
)
async def admin_server_health(current_admin: CurrentAdmin):
    import asyncio
    # cpu_percent has a blocking interval=0.5 — run in thread to avoid blocking event loop
    loop   = asyncio.get_event_loop()
    health = await loop.run_in_executor(None, _widget_server_health)
    return {"success": True, "server_health": health}


@router.get(
    "/new-products",
    summary     = "New products — adjustable look-back window",
    description = (
        "Widget 6 with a configurable look-back window.\n\n"
        "`window_hours` defaults to 24. "
        "Pass `?window_hours=168` for the last 7 days, etc."
    ),
)
async def admin_new_products(
    current_admin: CurrentAdmin,
    window_hours:  int = 24,
):
    if window_hours < 1 or window_hours > 8760:
        raise HTTPException(
            status_code=400,
            detail="window_hours must be between 1 and 8760 (1 year).",
        )
    data = await _widget_new_products(window_hours=window_hours)
    return {"success": True, **data}
