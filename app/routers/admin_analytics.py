# routers/admin_analytics.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Admin Analytics                             ║
║                                                                  ║
║  Endpoints:                                                      ║
║   GET /admin/analytics/users       → user demographics          ║
║   GET /admin/analytics/scans       → scan analytics             ║
║   GET /admin/analytics/engagement  → engagement metrics         ║
║   GET /admin/analytics/export      → CSV export                 ║
║                                                                  ║
║  Data sources (read-only aggregations):                         ║
║   users        → skin_type, hair_type, skin_concerns,          ║
║                  hair_concerns, current_phase, allergies,       ║
║                  budget, created_at                              ║
║   scan_results → scan_type, score, detected_triggers,          ║
║                  scanned_at, is_mock                            ║
║   subscriptions→ rc_entitlement_id (plan type)                  ║
║                                                                  ║
║  All pipelines are read-only — no writes are performed.         ║
║  Auth: Admin JWT (same secret as other /admin/* routes).        ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.database import get_db
from app.routers.admin_auth import _get_current_admin

logger = logging.getLogger(__name__)
router       = APIRouter(prefix="/admin/analytics", tags=["Admin Analytics"])
CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPER — date window
# ═══════════════════════════════════════════════════════════════════════════════

def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def _safe_pct(part: int, total: int) -> float:
    return round(part / total * 100, 1) if total else 0.0


_PERIODS = {
    "last_7_days":  ("Last 7 Days", 7),
    "last_30_days": ("Last 30 Days", 30),
    "last_90_days": ("Last 90 Days", 90),
    "last_6_months": ("Last 6 Months", 183),
    "last_1_year": ("Last 1 Year", 365),
    "all_time": ("All Time", None),
}

_PERIOD_ALIASES = {
    "7d": "last_7_days",
    "last7days": "last_7_days",
    "last7": "last_7_days",
    "30d": "last_30_days",
    "last30days": "last_30_days",
    "last30": "last_30_days",
    "90d": "last_90_days",
    "last90days": "last_90_days",
    "last90": "last_90_days",
    "6m": "last_6_months",
    "last6months": "last_6_months",
    "1y": "last_1_year",
    "365d": "last_1_year",
    "last1year": "last_1_year",
    "all": "all_time",
    "alltime": "all_time",
}

_AGE_GROUPS = {
    "all": ("All Ages", None, None),
    "18-24": ("18-24", 18, 24),
    "25-34": ("25-34", 25, 34),
    "35-44": ("35-44", 35, 44),
    "45-54": ("45-54", 45, 54),
    "55_plus": ("55+", 55, None),
}

_AGE_ALIASES = {
    "allages": "all",
    "all": "all",
    "55+": "55_plus",
    "55plus": "55_plus",
    "55": "55_plus",
}

_GENDERS = {
    "all": "All Genders",
    "male": "Male",
    "female": "Female",
    "other": "Other",
}

_GENDER_ALIASES = {
    "all": "all",
    "allgenders": "all",
    "men": "male",
    "man": "male",
    "m": "male",
    "women": "female",
    "woman": "female",
    "f": "female",
}

_SKIN_CONCERN_LABELS = {
    "acne_pimple": "Acne",
    "acne": "Acne",
    "pimple": "Acne",
    "redness": "Redness",
    "dryness": "Dryness",
    "dry_skin": "Dryness",
    "aging": "Aging",
    "fine_lines": "Aging",
    "wrinkles": "Aging",
    "pigmentation": "Pigmentation",
    "hyperpigmentation": "Pigmentation",
    "dark_spots": "Pigmentation",
}


def _compact_token(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )


def _label_from_key(value: str | None) -> str:
    if not value:
        return "Unknown"
    return str(value).replace("_", " ").replace("-", " ").title()


def _normalize_period(value: str) -> str:
    raw = str(value or "last_30_days").strip().lower().replace(" ", "_")
    if raw in _PERIODS:
        return raw
    alias = _PERIOD_ALIASES.get(_compact_token(value))
    if alias:
        return alias
    raise HTTPException(status_code=400, detail="Invalid period filter.")


def _normalize_age_group(value: str) -> str:
    raw = str(value or "all").strip().lower().replace(" ", "_").replace("+", "_plus")
    if raw in _AGE_GROUPS:
        return raw
    alias = _AGE_ALIASES.get(_compact_token(value)) or _AGE_ALIASES.get(str(value or "").strip().lower())
    if alias:
        return alias
    if str(value or "").strip() in _AGE_GROUPS:
        return str(value).strip()
    raise HTTPException(status_code=400, detail="Invalid age_group filter.")


def _normalize_gender(value: str) -> str:
    raw = str(value or "all").strip().lower()
    if raw in _GENDERS:
        return raw
    alias = _GENDER_ALIASES.get(_compact_token(value))
    if alias:
        return alias
    raise HTTPException(status_code=400, detail="Invalid gender filter.")


def _period_since(period: str) -> datetime | None:
    _, days = _PERIODS[period]
    return _days_ago(days) if days is not None else None


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _age_from_dob(value) -> int | None:
    dob = _parse_date(value)
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _matches_age_group(user: dict, age_group: str) -> bool:
    _, min_age, max_age = _AGE_GROUPS[age_group]
    if min_age is None:
        return True
    age = _age_from_dob(user.get("date_of_birth"))
    if age is None:
        return False
    if age < min_age:
        return False
    return max_age is None or age <= max_age


def _distribution_from_counts(counts: dict[str, int], total: int) -> List["DistributionItem"]:
    return [
        DistributionItem(label=label, count=count, percent=_safe_pct(count, total))
        for label, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class DistributionItem(BaseModel):
    label:   str
    count:   int
    percent: float


class FilterOption(BaseModel):
    value: str
    label: str


class AppliedUserAnalyticsFilters(BaseModel):
    period: str
    period_label: str
    age_group: str
    age_group_label: str
    gender: str
    gender_label: str


class TriggerFrequencyItem(BaseModel):
    trigger_name:  str
    count:         int
    percent:       float
    avg_level:     str   # dominant level across occurrences: low | medium | high


class DailyCountItem(BaseModel):
    date:  str   # YYYY-MM-DD
    count: int


class ScoreRangeItem(BaseModel):
    range:   str   # e.g. "0-20"
    count:   int
    percent: float


# ── /users response ────────────────────────────────────────────────────────────

class UserAnalyticsResponse(BaseModel):
    # Totals
    total_users:          int
    new_users_last_7d:    int
    new_users_last_30d:   int
    users_with_profile:   int   # have completed onboarding (skin_type set)

    # Profile distributions
    skin_type_distribution:    List[DistributionItem]
    skin_concern_distribution: List[DistributionItem]
    hair_type_distribution:    List[DistributionItem]
    hair_concern_distribution: List[DistributionItem]
    phase_distribution:        List[DistributionItem]
    budget_distribution:       List[DistributionItem]

    # Allergy stats
    users_with_allergies:      int
    top_allergies:             List[DistributionItem]

    # Sign-up trend (last 30 days)
    signup_trend:              List[DailyCountItem]


class AdminUserAnalyticsDashboardResponse(BaseModel):
    filters: AppliedUserAnalyticsFilters
    filter_options: dict[str, List[FilterOption]]
    total_users: int
    users_with_profile: int
    top_skin_concerns: List[DistributionItem]
    skin_type_distribution: List[DistributionItem]
    gender_distribution: List[DistributionItem]
    age_distribution: List[DistributionItem]
    signup_trend: List[DailyCountItem]


# ── /scans response ────────────────────────────────────────────────────────────

class ScanAnalyticsResponse(BaseModel):
    # Totals (excludes mock scans)
    total_scans:           int
    face_scans:            int
    hair_scalp_scans:      int
    product_scans:         int

    # Score stats per type
    avg_face_score:         Optional[float]
    avg_hair_scalp_score:   Optional[float]

    face_score_distribution:      List[ScoreRangeItem]
    hair_scalp_score_distribution: List[ScoreRangeItem]

    # Top triggers (face)
    top_face_triggers:       List[TriggerFrequencyItem]
    # Top triggers (hair/scalp)
    top_hair_triggers:       List[TriggerFrequencyItem]

    # Scan trend (last 30 days, all types combined)
    scan_trend:              List[DailyCountItem]


# ── /engagement response ───────────────────────────────────────────────────────

class EngagementAnalyticsResponse(BaseModel):
    # User activity
    users_with_at_least_1_scan: int
    users_with_3_plus_scans:    int   # returning / regular users
    avg_scans_per_user:         float

    # Scan recency
    active_users_last_7d:   int   # unique users who scanned in last 7 days
    active_users_last_30d:  int   # unique users who scanned in last 30 days

    # Subscription split
    premium_users:  int
    basic_users:    int

    # Routine adoption
    users_with_routine_steps: int


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPER — score bucket
# ═══════════════════════════════════════════════════════════════════════════════

_SCORE_RANGES = [
    ("0–20",   0,  20),
    ("21–40",  21, 40),
    ("41–60",  41, 60),
    ("61–80",  61, 80),
    ("81–100", 81, 100),
]


def _bucket_scores(scores: list[int]) -> List[ScoreRangeItem]:
    buckets: dict[str, int] = {r[0]: 0 for r in _SCORE_RANGES}
    for s in scores:
        for label, lo, hi in _SCORE_RANGES:
            if lo <= s <= hi:
                buckets[label] += 1
                break
    total = len(scores)
    return [
        ScoreRangeItem(range=label, count=cnt, percent=_safe_pct(cnt, total))
        for label, cnt in buckets.items()
    ]


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPER — trigger level dominant
#  Given a list of levels ["low","medium","high",...] returns the most frequent.
# ═══════════════════════════════════════════════════════════════════════════════

def _dominant_level(levels: list[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for lv in levels:
        counts[lv.lower()] += 1
    order = ["high", "medium", "low"]
    # Prefer highest severity in a tie
    return max(order, key=lambda x: (counts.get(x, 0), order.index(x) * -1))


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 1 — USER DEMOGRAPHICS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/users",
    response_model=UserAnalyticsResponse,
    summary="User Demographics Analytics",
    description=(
        "Returns aggregated user demographic data for the admin dashboard.\n\n"
        "**Includes:**\n"
        "- Total users, new signups (7d / 30d)\n"
        "- Skin type & concern distribution\n"
        "- Hair type & concern distribution\n"
        "- Hormonal phase distribution\n"
        "- Budget tier distribution\n"
        "- Allergy prevalence & top allergens\n"
        "- Daily signup trend (last 30 days)\n\n"
        "Only users who have completed onboarding (`skin_type` set) are counted "
        "in distribution charts. Raw totals include all registered accounts."
    ),
)
async def get_user_analytics(
    _: CurrentAdmin,
) -> UserAnalyticsResponse:
    db = get_db()

    # ── 1. Basic counts ────────────────────────────────────────────────────────
    total_users = await db.users.count_documents({})
    now         = datetime.now(timezone.utc)

    new_7d  = await db.users.count_documents({"created_at": {"$gte": _days_ago(7)}})
    new_30d = await db.users.count_documents({"created_at": {"$gte": _days_ago(30)}})
    with_profile = await db.users.count_documents(
        {"skin_type": {"$exists": True, "$ne": None}}
    )

    # ── 2. Skin type distribution ──────────────────────────────────────────────
    skin_type_pipeline = [
        {"$match": {"skin_type": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$skin_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    skin_type_raw = await db.users.aggregate(skin_type_pipeline).to_list(length=20)
    skin_type_total = sum(d["count"] for d in skin_type_raw)
    skin_type_dist = [
        DistributionItem(
            label=d["_id"],
            count=d["count"],
            percent=_safe_pct(d["count"], skin_type_total),
        )
        for d in skin_type_raw
    ]

    # ── 3. Skin concern distribution ─────────────────────────────────────────
    # skin_concerns is a list field → unwind to count each concern individually
    skin_concern_pipeline = [
        {"$match": {"skin_concerns": {"$exists": True, "$ne": []}}},
        {"$unwind": "$skin_concerns"},
        {"$group": {"_id": "$skin_concerns", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    skin_concern_raw = await db.users.aggregate(skin_concern_pipeline).to_list(length=30)
    sc_total = sum(d["count"] for d in skin_concern_raw)
    skin_concern_dist = [
        DistributionItem(
            label=d["_id"].replace("_", " ").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], sc_total),
        )
        for d in skin_concern_raw
    ]

    # ── 4. Hair type distribution ─────────────────────────────────────────────
    hair_type_pipeline = [
        {"$match": {"hair_type": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$hair_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    hair_type_raw = await db.users.aggregate(hair_type_pipeline).to_list(length=20)
    ht_total = sum(d["count"] for d in hair_type_raw)
    hair_type_dist = [
        DistributionItem(
            label=d["_id"].replace("_", "/").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], ht_total),
        )
        for d in hair_type_raw
    ]

    # ── 5. Hair concern distribution ──────────────────────────────────────────
    hair_concern_pipeline = [
        {"$match": {"hair_concerns": {"$exists": True, "$ne": []}}},
        {"$unwind": "$hair_concerns"},
        {"$group": {"_id": "$hair_concerns", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    hair_concern_raw = await db.users.aggregate(hair_concern_pipeline).to_list(length=20)
    hc_total = sum(d["count"] for d in hair_concern_raw)
    hair_concern_dist = [
        DistributionItem(
            label=d["_id"].replace("_", " ").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], hc_total),
        )
        for d in hair_concern_raw
    ]

    # ── 6. Hormonal phase distribution ───────────────────────────────────────
    phase_pipeline = [
        {"$match": {"current_phase": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$current_phase", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    phase_raw = await db.users.aggregate(phase_pipeline).to_list(length=10)
    ph_total  = sum(d["count"] for d in phase_raw)
    phase_dist = [
        DistributionItem(
            label=d["_id"].replace("_", " ").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], ph_total),
        )
        for d in phase_raw
    ]

    # ── 7. Budget distribution ────────────────────────────────────────────────
    budget_pipeline = [
        {"$match": {"budget": {"$exists": True, "$ne": None}}},
        {"$group": {"_id": "$budget", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    budget_raw = await db.users.aggregate(budget_pipeline).to_list(length=10)
    bg_total   = sum(d["count"] for d in budget_raw)
    budget_dist = [
        DistributionItem(
            label=d["_id"].replace("_", " ").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], bg_total),
        )
        for d in budget_raw
    ]

    # ── 8. Allergy stats ──────────────────────────────────────────────────────
    users_with_allergies = await db.users.count_documents(
        {"allergies": {"$exists": True, "$not": {"$size": 0}}}
    )
    allergy_pipeline = [
        {"$match": {"allergies": {"$exists": True, "$ne": []}}},
        {"$unwind": "$allergies"},
        {"$group": {"_id": "$allergies", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    allergy_raw  = await db.users.aggregate(allergy_pipeline).to_list(length=10)
    al_total     = sum(d["count"] for d in allergy_raw)
    allergy_dist = [
        DistributionItem(
            label=d["_id"].replace("_", " ").title(),
            count=d["count"],
            percent=_safe_pct(d["count"], al_total),
        )
        for d in allergy_raw
    ]

    # ── 9. Signup trend (last 30 days, daily) ─────────────────────────────────
    signup_trend_pipeline = [
        {"$match": {"created_at": {"$gte": _days_ago(30)}}},
        {
            "$group": {
                "_id": {
                    "y": {"$year":  "$created_at"},
                    "m": {"$month": "$created_at"},
                    "d": {"$dayOfMonth": "$created_at"},
                },
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id.y": 1, "_id.m": 1, "_id.d": 1}},
    ]
    signup_trend_raw = await db.users.aggregate(signup_trend_pipeline).to_list(length=31)
    signup_trend = [
        DailyCountItem(
            date=f"{d['_id']['y']:04d}-{d['_id']['m']:02d}-{d['_id']['d']:02d}",
            count=d["count"],
        )
        for d in signup_trend_raw
    ]

    return UserAnalyticsResponse(
        total_users=total_users,
        new_users_last_7d=new_7d,
        new_users_last_30d=new_30d,
        users_with_profile=with_profile,
        skin_type_distribution=skin_type_dist,
        skin_concern_distribution=skin_concern_dist,
        hair_type_distribution=hair_type_dist,
        hair_concern_distribution=hair_concern_dist,
        phase_distribution=phase_dist,
        budget_distribution=budget_dist,
        users_with_allergies=users_with_allergies,
        top_allergies=allergy_dist,
        signup_trend=signup_trend,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINT 2 — SCAN ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_options() -> dict[str, List[FilterOption]]:
    return {
        "periods": [
            FilterOption(value=key, label=label)
            for key, (label, _) in _PERIODS.items()
        ],
        "age_groups": [
            FilterOption(value=key, label=label)
            for key, (label, _, _) in _AGE_GROUPS.items()
        ],
        "genders": [
            FilterOption(value=key, label=label)
            for key, label in _GENDERS.items()
        ],
    }


def _applied_filters(period: str, age_group: str, gender: str) -> AppliedUserAnalyticsFilters:
    return AppliedUserAnalyticsFilters(
        period=period,
        period_label=_PERIODS[period][0],
        age_group=age_group,
        age_group_label=_AGE_GROUPS[age_group][0],
        gender=gender,
        gender_label=_GENDERS[gender],
    )


async def _load_user_analytics_rows(
    period: str,
    age_group: str,
    gender: str,
) -> list[dict]:
    db = get_db()
    query: dict = {}
    since = _period_since(period)
    if since:
        query["created_at"] = {"$gte": since}
    if gender != "all":
        query["gender"] = gender

    rows = await db.users.find(
        query,
        {
            "_id": 1,
            "email": 1,
            "full_name": 1,
            "created_at": 1,
            "date_of_birth": 1,
            "gender": 1,
            "skin_type": 1,
            "skin_concerns": 1,
            "hair_type": 1,
            "hair_concerns": 1,
            "current_phase": 1,
            "budget": 1,
            "onboarding_completed": 1,
        },
    ).sort("created_at", -1).to_list(length=100_000)

    return [row for row in rows if _matches_age_group(row, age_group)]


def _dashboard_from_rows(
    rows: list[dict],
    period: str,
    age_group: str,
    gender: str,
) -> AdminUserAnalyticsDashboardResponse:
    skin_concern_counts: dict[str, int] = defaultdict(int)
    skin_type_counts: dict[str, int] = defaultdict(int)
    gender_counts: dict[str, int] = defaultdict(int)
    age_counts: dict[str, int] = defaultdict(int)
    signup_counts: dict[str, int] = defaultdict(int)

    users_with_profile = 0

    for row in rows:
        if row.get("skin_type"):
            users_with_profile += 1
            skin_type_counts[_label_from_key(row.get("skin_type"))] += 1

        gender_counts[_GENDERS.get(str(row.get("gender") or "").lower(), "Unknown")] += 1

        age = _age_from_dob(row.get("date_of_birth"))
        if age is not None:
            for key, (label, min_age, max_age) in _AGE_GROUPS.items():
                if key == "all":
                    continue
                if age >= (min_age or 0) and (max_age is None or age <= max_age):
                    age_counts[label] += 1
                    break

        created = _parse_date(row.get("created_at"))
        if created:
            signup_counts[created.isoformat()] += 1

        for concern in row.get("skin_concerns") or []:
            raw = str(concern or "").strip().lower()
            if not raw:
                continue
            label = _SKIN_CONCERN_LABELS.get(raw, _label_from_key(raw))
            skin_concern_counts[label] += 1

    skin_concern_total = sum(skin_concern_counts.values())
    skin_type_total = sum(skin_type_counts.values())
    gender_total = sum(gender_counts.values())
    age_total = sum(age_counts.values())

    signup_trend = [
        DailyCountItem(date=day, count=count)
        for day, count in sorted(signup_counts.items())
    ]

    return AdminUserAnalyticsDashboardResponse(
        filters=_applied_filters(period, age_group, gender),
        filter_options=_filter_options(),
        total_users=len(rows),
        users_with_profile=users_with_profile,
        top_skin_concerns=_distribution_from_counts(skin_concern_counts, skin_concern_total)[:10],
        skin_type_distribution=_distribution_from_counts(skin_type_counts, skin_type_total),
        gender_distribution=_distribution_from_counts(gender_counts, gender_total),
        age_distribution=_distribution_from_counts(age_counts, age_total),
        signup_trend=signup_trend,
    )


@router.get(
    "/user-dashboard",
    response_model=AdminUserAnalyticsDashboardResponse,
    summary="Admin User Analytics Dashboard",
    description=(
        "Returns chart-ready user analytics for the admin User Analytics page.\n\n"
        "Filters match the dashboard controls: period, age group, and gender.\n"
        "Accepted period examples: last_30_days, Last 30 Days, 30d, all_time.\n"
        "Accepted age_group examples: all, 25-34, 55+.\n"
        "Accepted gender examples: all, male, female, other."
    ),
)
async def get_admin_user_analytics_dashboard(
    _: CurrentAdmin,
    period: str = Query(default="last_30_days"),
    age_group: str = Query(default="all"),
    gender: str = Query(default="all"),
) -> AdminUserAnalyticsDashboardResponse:
    period_key = _normalize_period(period)
    age_key = _normalize_age_group(age_group)
    gender_key = _normalize_gender(gender)
    rows = await _load_user_analytics_rows(period_key, age_key, gender_key)
    return _dashboard_from_rows(rows, period_key, age_key, gender_key)


@router.get(
    "/user-dashboard/export",
    summary="Export Admin User Analytics Report",
    responses={
        200: {
            "content": {"text/csv": {}},
            "description": "CSV file download",
        }
    },
)
async def export_admin_user_analytics_dashboard(
    _: CurrentAdmin,
    period: str = Query(default="last_30_days"),
    age_group: str = Query(default="all"),
    gender: str = Query(default="all"),
) -> StreamingResponse:
    period_key = _normalize_period(period)
    age_key = _normalize_age_group(age_group)
    gender_key = _normalize_gender(gender)
    rows = await _load_user_analytics_rows(period_key, age_key, gender_key)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "user_id",
        "email",
        "full_name",
        "created_at",
        "age",
        "gender",
        "skin_type",
        "skin_concerns",
        "hair_type",
        "hair_concerns",
        "current_phase",
        "budget",
        "onboarding_completed",
    ])

    for row in rows:
        created_at = row.get("created_at", "")
        writer.writerow([
            str(row.get("_id", "")),
            row.get("email", ""),
            row.get("full_name", ""),
            created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
            _age_from_dob(row.get("date_of_birth")) or "",
            row.get("gender", ""),
            row.get("skin_type", ""),
            "; ".join(row.get("skin_concerns") or []),
            row.get("hair_type", ""),
            "; ".join(row.get("hair_concerns") or []),
            row.get("current_phase", ""),
            row.get("budget", ""),
            bool(row.get("onboarding_completed", False)),
        ])

    output.seek(0)
    filename = f"skinsense_user_analytics_{period_key}_{age_key}_{gender_key}_{_today_str()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# @router.get(
#     "/scans",
#     response_model=ScanAnalyticsResponse,
#     summary="Scan Analytics",
#     description=(
#         "Returns aggregated scan data from `scan_results` for the admin dashboard.\n\n"
#         "**Includes:**\n"
#         "- Total scans by type (face / hair_scalp / product)\n"
#         "- Average AI scores per scan type\n"
#         "- Score distribution histograms (0-20, 21-40 … 81-100)\n"
#         "- Top detected trigger names for face & hair/scalp scans\n"
#         "- Daily scan trend (last 30 days)\n\n"
#         "Mock scans (`is_mock: true`) are excluded from all counts and averages "
#         "unless `include_mock=true` is passed as a query parameter."
#     ),
# )
# async def get_scan_analytics(
#     _: CurrentAdmin,
#     include_mock: bool = Query(
#         default=False,
#         description="If true, include mock scans (MOCK_MODE results) in analytics.",
#     ),
# ) -> ScanAnalyticsResponse:
#     db   = get_db()
#     base = {} if include_mock else {"is_mock": {"$ne": True}}

#     # ── 1. Total counts by scan type ──────────────────────────────────────────
#     count_pipeline = [
#         {"$match": base},
#         {"$group": {"_id": "$scan_type", "count": {"$sum": 1}}},
#     ]
#     count_raw = await db.scan_results.aggregate(count_pipeline).to_list(length=10)
#     counts: dict[str, int] = {d["_id"]: d["count"] for d in count_raw}
#     total_scans      = sum(counts.values())
#     face_scans       = counts.get("face",       0)
#     hair_scalp_scans = counts.get("hair_scalp", 0)
#     product_scans    = counts.get("product",    0)

#     # ── 2. Average scores ─────────────────────────────────────────────────────
#     async def _avg_score(scan_type: str) -> Optional[float]:
#         pipeline = [
#             {"$match": {**base, "scan_type": scan_type, "score": {"$exists": True}}},
#             {"$group": {"_id": None, "avg": {"$avg": "$score"}}},
#         ]
#         res = await db.scan_results.aggregate(pipeline).to_list(length=1)
#         return round(res[0]["avg"], 1) if res else None

#     avg_face_score       = await _avg_score("face")
#     avg_hair_scalp_score = await _avg_score("hair_scalp")

#     # ── 3. Score distributions ────────────────────────────────────────────────
#     async def _fetch_scores(scan_type: str) -> list[int]:
#         docs = await db.scan_results.find(
#             {**base, "scan_type": scan_type, "score": {"$exists": True}},
#             {"score": 1},
#         ).to_list(length=10_000)
#         return [d["score"] for d in docs if isinstance(d.get("score"), (int, float))]

#     face_scores       = await _fetch_scores("face")
#     hair_scalp_scores = await _fetch_scores("hair_scalp")

#     # ── 4. Top triggers ───────────────────────────────────────────────────────
#     async def _top_triggers(scan_type: str, limit: int = 10) -> List[TriggerFrequencyItem]:
#         """
#         Unwinds detected_triggers, groups by trigger_name, counts occurrences
#         and collects all observed trigger_level values to determine dominance.
#         """
#         pipeline = [
#             {"$match": {**base, "scan_type": scan_type}},
#             {"$unwind": "$detected_triggers"},
#             {
#                 "$group": {
#                     "_id":    "$detected_triggers.trigger_name",
#                     "count":  {"$sum": 1},
#                     "levels": {"$push": "$detected_triggers.trigger_level"},
#                 }
#             },
#             {"$sort": {"count": -1}},
#             {"$limit": limit},
#         ]
#         raw   = await db.scan_results.aggregate(pipeline).to_list(length=limit)
#         total = sum(d["count"] for d in raw)
#         return [
#             TriggerFrequencyItem(
#                 trigger_name=d["_id"],
#                 count=d["count"],
#                 percent=_safe_pct(d["count"], total),
#                 avg_level=_dominant_level(d.get("levels", [])),
#             )
#             for d in raw
#         ]

#     top_face_triggers = await _top_triggers("face")
#     top_hair_triggers = await _top_triggers("hair_scalp")

#     # ── 5. Scan trend (last 30 days) ──────────────────────────────────────────
#     scan_trend_pipeline = [
#         {"$match": {**base, "scanned_at": {"$gte": _days_ago(30)}}},
#         {
#             "$group": {
#                 "_id": {
#                     "y": {"$year":       "$scanned_at"},
#                     "m": {"$month":      "$scanned_at"},
#                     "d": {"$dayOfMonth": "$scanned_at"},
#                 },
#                 "count": {"$sum": 1},
#             }
#         },
#         {"$sort": {"_id.y": 1, "_id.m": 1, "_id.d": 1}},
#     ]
#     scan_trend_raw = await db.scan_results.aggregate(scan_trend_pipeline).to_list(length=31)
#     scan_trend = [
#         DailyCountItem(
#             date=f"{d['_id']['y']:04d}-{d['_id']['m']:02d}-{d['_id']['d']:02d}",
#             count=d["count"],
#         )
#         for d in scan_trend_raw
#     ]

#     return ScanAnalyticsResponse(
#         total_scans=total_scans,
#         face_scans=face_scans,
#         hair_scalp_scans=hair_scalp_scans,
#         product_scans=product_scans,
#         avg_face_score=avg_face_score,
#         avg_hair_scalp_score=avg_hair_scalp_score,
#         face_score_distribution=_bucket_scores(face_scores),
#         hair_scalp_score_distribution=_bucket_scores(hair_scalp_scores),
#         top_face_triggers=top_face_triggers,
#         top_hair_triggers=top_hair_triggers,
#         scan_trend=scan_trend,
#     )


# # ═══════════════════════════════════════════════════════════════════════════════
# #  ENDPOINT 3 — ENGAGEMENT METRICS
# # ═══════════════════════════════════════════════════════════════════════════════

# @router.get(
#     "/engagement",
#     response_model=EngagementAnalyticsResponse,
#     summary="User Engagement Metrics",
#     description=(
#         "Returns user engagement and activity metrics.\n\n"
#         "**Includes:**\n"
#         "- Users who have done at least 1 / 3+ scans\n"
#         "- Average scans per registered user\n"
#         "- Active users in the last 7 and 30 days (unique users who scanned)\n"
#         "- Premium vs basic subscription split\n"
#         "- Routine adoption (users with at least one routine step saved)\n\n"
#         "Mock scans are excluded from all scan-based engagement metrics."
#     ),
# )
# async def get_engagement_analytics(
#     _: CurrentAdmin,
# ) -> EngagementAnalyticsResponse:
#     db = get_db()

#     # ── Scan-based engagement (exclude mocks) ─────────────────────────────────
#     base = {"is_mock": {"$ne": True}}

#     # Users with ≥1 scan
#     scan_per_user_pipeline = [
#         {"$match": base},
#         {"$group": {"_id": "$user_id", "scan_count": {"$sum": 1}}},
#     ]
#     scan_per_user = await db.scan_results.aggregate(scan_per_user_pipeline).to_list(
#         length=100_000
#     )
#     users_with_scans     = len(scan_per_user)
#     users_3_plus         = sum(1 for d in scan_per_user if d["scan_count"] >= 3)
#     total_scan_count     = sum(d["scan_count"] for d in scan_per_user)
#     total_users          = await db.users.count_documents({})
#     avg_scans_per_user   = round(total_scan_count / total_users, 2) if total_users else 0.0

#     # Active users — last 7 days
#     active_7d_pipeline = [
#         {"$match": {**base, "scanned_at": {"$gte": _days_ago(7)}}},
#         {"$group": {"_id": "$user_id"}},
#         {"$count": "total"},
#     ]
#     res_7d         = await db.scan_results.aggregate(active_7d_pipeline).to_list(length=1)
#     active_users_7d = res_7d[0]["total"] if res_7d else 0

#     # Active users — last 30 days
#     active_30d_pipeline = [
#         {"$match": {**base, "scanned_at": {"$gte": _days_ago(30)}}},
#         {"$group": {"_id": "$user_id"}},
#         {"$count": "total"},
#     ]
#     res_30d          = await db.scan_results.aggregate(active_30d_pipeline).to_list(length=1)
#     active_users_30d = res_30d[0]["total"] if res_30d else 0

#     # ── Subscription split ─────────────────────────────────────────────────────
#     # rc_entitlement_id holds the RevenueCat entitlement identifier.
#     # "premium" entitlement → premium plan; anything else → basic.
#     premium_users = await db.subscriptions.count_documents(
#         {"rc_entitlement_id": "premium"}
#     )
#     basic_users = await db.subscriptions.count_documents(
#         {"rc_entitlement_id": {"$ne": "premium"}}
#     )

#     # ── Routine adoption ──────────────────────────────────────────────────────
#     routine_pipeline = [
#         {"$group": {"_id": "$user_id"}},
#         {"$count": "total"},
#     ]
#     routine_res = await db.routine_steps.aggregate(routine_pipeline).to_list(length=1)
#     users_with_routine = routine_res[0]["total"] if routine_res else 0

#     return EngagementAnalyticsResponse(
#         users_with_at_least_1_scan=users_with_scans,
#         users_with_3_plus_scans=users_3_plus,
#         avg_scans_per_user=avg_scans_per_user,
#         active_users_last_7d=active_users_7d,
#         active_users_last_30d=active_users_30d,
#         premium_users=premium_users,
#         basic_users=basic_users,
#         users_with_routine_steps=users_with_routine,
#     )


# # ═══════════════════════════════════════════════════════════════════════════════
# #  ENDPOINT 4 — CSV EXPORT
# # ═══════════════════════════════════════════════════════════════════════════════

# @router.get(
#     "/export",
#     summary="Export Analytics as CSV",
#     description=(
#         "Streams a CSV file for download.\n\n"
#         "**`type` options:**\n"
#         "- `users`   → one row per user (email, skin_type, hair_type, concerns, "
#         "phase, budget, signup date)\n"
#         "- `scans`   → one row per scan result (user_id, scan_type, score, "
#         "top trigger, scanned_at)\n"
#         "- `triggers`→ one row per detected trigger across all scans "
#         "(trigger_name, level, scan_type, scanned_at)\n\n"
#         "File is streamed as `text/csv` with UTF-8 encoding. "
#         "Filename is set in the `Content-Disposition` header."
#     ),
#     responses={
#         200: {
#             "content": {"text/csv": {}},
#             "description": "CSV file download",
#         }
#     },
# )
# async def export_analytics_csv(
#     _: CurrentAdmin,
#     type: str = Query(
#         default="users",
#         description="Export type: 'users' | 'scans' | 'triggers'",
#         pattern="^(users|scans|triggers)$",
#     ),
#     days: int = Query(
#         default=90,
#         ge=1,
#         le=365,
#         description="Export data from the last N days (max 365).",
#     ),
# ) -> StreamingResponse:

#     db      = get_db()
#     since   = _days_ago(days)
#     output  = io.StringIO()
#     writer  = csv.writer(output)

#     if type == "users":
#         # ── Users export ───────────────────────────────────────────────────────
#         filename = f"skinsense_users_{_today_str()}.csv"
#         writer.writerow([
#             "user_id", "email", "full_name",
#             "skin_type", "skin_concerns",
#             "hair_type", "hair_concerns",
#             "current_phase", "budget",
#             "has_allergies", "allergies",
#             "created_at",
#         ])

#         users = await db.users.find(
#             {"created_at": {"$gte": since}},
#             {
#                 "_id": 1, "email": 1, "full_name": 1,
#                 "skin_type": 1, "skin_concerns": 1,
#                 "hair_type": 1, "hair_concerns": 1,
#                 "current_phase": 1, "budget": 1,
#                 "allergies": 1, "created_at": 1,
#             },
#         ).sort("created_at", -1).to_list(length=50_000)

#         for u in users:
#             allergies     = u.get("allergies") or []
#             skin_concerns = u.get("skin_concerns") or []
#             hair_concerns = u.get("hair_concerns") or []
#             created_at    = u.get("created_at", "")
#             writer.writerow([
#                 str(u["_id"]),
#                 u.get("email", ""),
#                 u.get("full_name", ""),
#                 u.get("skin_type", ""),
#                 "; ".join(skin_concerns),
#                 u.get("hair_type", ""),
#                 "; ".join(hair_concerns),
#                 u.get("current_phase", ""),
#                 u.get("budget", ""),
#                 "yes" if allergies else "no",
#                 "; ".join(allergies),
#                 created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
#             ])

#     elif type == "scans":
#         # ── Scans export ───────────────────────────────────────────────────────
#         filename = f"skinsense_scans_{_today_str()}.csv"
#         writer.writerow([
#             "scan_id", "user_id", "scan_type",
#             "score", "advice",
#             "trigger_count",
#             "top_trigger", "top_trigger_level",
#             "is_mock", "scanned_at",
#         ])

#         scans = await db.scan_results.find(
#             {"scanned_at": {"$gte": since}},
#             {
#                 "_id": 1, "user_id": 1, "scan_type": 1,
#                 "score": 1, "advice": 1,
#                 "detected_triggers": 1,
#                 "is_mock": 1, "scanned_at": 1,
#             },
#         ).sort("scanned_at", -1).to_list(length=50_000)

#         for s in scans:
#             triggers    = s.get("detected_triggers") or []
#             top         = triggers[0] if triggers else {}
#             scanned_at  = s.get("scanned_at", "")
#             writer.writerow([
#                 str(s["_id"]),
#                 s.get("user_id", ""),
#                 s.get("scan_type", ""),
#                 s.get("score", ""),
#                 (s.get("advice") or "").replace("\n", " "),
#                 len(triggers),
#                 top.get("trigger_name", ""),
#                 top.get("trigger_level", ""),
#                 "yes" if s.get("is_mock") else "no",
#                 scanned_at.isoformat() if isinstance(scanned_at, datetime) else str(scanned_at),
#             ])

#     else:  # triggers
#         # ── Trigger-level export (one row per detected trigger) ────────────────
#         filename = f"skinsense_triggers_{_today_str()}.csv"
#         writer.writerow([
#             "scan_id", "user_id", "scan_type",
#             "scan_score",
#             "trigger_name", "trigger_level", "cure_advice",
#             "scanned_at",
#         ])

#         scans = await db.scan_results.find(
#             {"scanned_at": {"$gte": since}, "detected_triggers": {"$exists": True, "$ne": []}},
#             {
#                 "_id": 1, "user_id": 1, "scan_type": 1,
#                 "score": 1, "detected_triggers": 1, "scanned_at": 1,
#             },
#         ).sort("scanned_at", -1).to_list(length=50_000)

#         for s in scans:
#             scanned_at = s.get("scanned_at", "")
#             ts_str     = (
#                 scanned_at.isoformat()
#                 if isinstance(scanned_at, datetime)
#                 else str(scanned_at)
#             )
#             for t in s.get("detected_triggers") or []:
#                 writer.writerow([
#                     str(s["_id"]),
#                     s.get("user_id", ""),
#                     s.get("scan_type", ""),
#                     s.get("score", ""),
#                     t.get("trigger_name", ""),
#                     t.get("trigger_level", ""),
#                     (t.get("cure_advice") or "").replace("\n", " "),
#                     ts_str,
#                 ])

#     output.seek(0)
#     return StreamingResponse(
#         iter([output.getvalue()]),
#         media_type="text/csv",
#         headers={"Content-Disposition": f'attachment; filename="{filename}"'},
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# #  Internal helper
# # ─────────────────────────────────────────────────────────────────────────────

# def _today_str() -> str:
#     return datetime.now(timezone.utc).strftime("%Y%m%d")
