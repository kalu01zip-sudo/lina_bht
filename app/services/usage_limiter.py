# app/services/usage_limiter.py

import logging
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, status
from app.core.database import get_db

logger = logging.getLogger(__name__)

# ── Default limits ────────────────────────────────────────────────────────────
# type: "count" → max_uses calls per period_days
# type: "token" → token_budget tokens per period_days
DEFAULT_LIMITS = {
    "face_scan": {
        "type": "count",
        "free_max_uses": 2,
        "free_period_days": 1,
        "premium_max_uses": 5,
        "premium_period_days": 1,
    },
    "scalp_scan": {
        "type": "count",
        "free_max_uses": 1,
        "free_period_days": 7,
        "premium_max_uses": 2,
        "premium_period_days": 1,
    },
    "lia_chat": {
        "type": "token",
        "free_token_budget": 10_000,
        "free_period_days": 7,
        "premium_token_budget": 100_000,
        "premium_period_days": 1,
    },
}

def _usage_logs_col():
    return get_db()["usage_logs"]

def _usage_limits_col():
    return get_db()["usage_limits"]


async def setup_usage_indexes():
    """Setup TTL index on usage logs and unique indexes."""
    try:
        await _usage_limits_col().create_index("_id", unique=True)
        # TTL: prune logs after 30 days
        await _usage_logs_col().create_index("timestamp", expireAfterSeconds=2592000)
        # Compound index for efficient queries
        await _usage_logs_col().create_index([
            ("user_id", 1), ("endpoint_id", 1), ("timestamp", -1)
        ])
        logger.info("[OK] Usage limiter indexes initialized successfully.")
    except Exception as e:
        logger.warning(f"[WARN] Failed to setup usage limiter indexes: {e}")


async def get_endpoint_config(endpoint_id: str) -> dict:
    """
    Get the full limit configuration for an endpoint.
    Checks the database first, falling back to DEFAULT_LIMITS.
    """
    defaults = DEFAULT_LIMITS.get(endpoint_id, {})
    try:
        config = await _usage_limits_col().find_one({"_id": endpoint_id})
        if config:
            # Merge: DB values override defaults
            merged = {**defaults}
            for key in config:
                if key != "_id":
                    merged[key] = config[key]
            return merged
    except Exception as e:
        logger.warning(f"Error reading usage limits for {endpoint_id}: {e}")

    return defaults


async def check_usage_limit(user_id: str, endpoint_id: str, plan: str) -> None:
    """
    Checks if the user has reached their tier limit for the given endpoint.
    Supports both count-based and token-based limits.
    Raises HTTPException 429 if the limit is exceeded.
    """
    config = await get_endpoint_config(endpoint_id)
    limit_type = config.get("type", "count")

    if limit_type == "token":
        await _check_token_limit(user_id, endpoint_id, plan, config)
    else:
        await _check_count_limit(user_id, endpoint_id, plan, config)


async def _check_count_limit(user_id: str, endpoint_id: str, plan: str, config: dict) -> None:
    """Check count-based limit: max_uses per period_days."""
    if plan == "premium":
        max_uses = config.get("premium_max_uses", 1)
        period_days = config.get("premium_period_days", 1)
    else:
        max_uses = config.get("free_max_uses", 1)
        period_days = config.get("free_period_days", 7)

    # 0 means unlimited
    if max_uses <= 0:
        return

    now = datetime.now(timezone.utc)
    since_date = (now - timedelta(days=period_days)).replace(tzinfo=None)

    # Count how many times the user has used this endpoint in the window
    use_count = await _usage_logs_col().count_documents({
        "user_id": str(user_id),
        "endpoint_id": endpoint_id,
        "timestamp": {"$gte": since_date}
    })

    if use_count >= max_uses:
        # Find the oldest log in this window to compute when the window expires
        oldest_log = await _usage_logs_col().find_one(
            {
                "user_id": str(user_id),
                "endpoint_id": endpoint_id,
                "timestamp": {"$gte": since_date}
            },
            sort=[("timestamp", 1)]
        )

        time_msg = _format_time_remaining(oldest_log, period_days, now)
        tier_name = "Premium" if plan == "premium" else "Free"
        limit_desc = f"{max_uses} time(s) every {period_days} day(s)" if period_days > 1 else f"{max_uses} time(s) daily"

        msg = (
            f"Usage limit reached for {endpoint_id.replace('_', ' ').title()}. "
            f"Your current tier ({tier_name}) is limited to {limit_desc}. "
            f"Please try again in {time_msg}."
        )

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=msg
        )


async def _check_token_limit(user_id: str, endpoint_id: str, plan: str, config: dict) -> None:
    """Check token-based limit: token_budget per period_days."""
    if plan == "premium":
        budget = config.get("premium_token_budget", 100_000)
        period_days = config.get("premium_period_days", 1)
    else:
        budget = config.get("free_token_budget", 10_000)
        period_days = config.get("free_period_days", 7)

    # 0 means unlimited
    if budget <= 0:
        return

    now = datetime.now(timezone.utc)
    since_date = (now - timedelta(days=period_days)).replace(tzinfo=None)

    # Sum all tokens used in the window
    pipeline = [
        {
            "$match": {
                "user_id": str(user_id),
                "endpoint_id": endpoint_id,
                "timestamp": {"$gte": since_date}
            }
        },
        {
            "$group": {
                "_id": None,
                "total_tokens": {"$sum": {"$ifNull": ["$tokens_used", 0]}},
                "oldest_timestamp": {"$min": "$timestamp"}
            }
        }
    ]

    result = await _usage_logs_col().aggregate(pipeline).to_list(length=1)

    if result:
        total_tokens = result[0].get("total_tokens", 0)
        if total_tokens >= budget:
            oldest_ts = result[0].get("oldest_timestamp")
            if oldest_ts:
                oldest_ts_utc = oldest_ts.replace(tzinfo=timezone.utc)
                next_available = oldest_ts_utc + timedelta(days=period_days)
                time_left = next_available - now
                hours_left = int(time_left.total_seconds() / 3600)
                if hours_left >= 24:
                    time_msg = f"{int(hours_left / 24)} day(s)"
                elif hours_left > 0:
                    time_msg = f"{hours_left} hour(s)"
                else:
                    minutes_left = max(1, int(time_left.total_seconds() / 60))
                    time_msg = f"{minutes_left} minute(s)"
            else:
                time_msg = f"{period_days} day(s)"

            tier_name = "Premium" if plan == "premium" else "Free"

            msg = (
                f"Token limit reached for {endpoint_id.replace('_', ' ').title()}. "
                f"Your current tier ({tier_name}) has used {total_tokens:,}/{budget:,} tokens "
                f"in the last {period_days} day(s). "
                f"Please try again in {time_msg}."
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=msg
            )


def _format_time_remaining(oldest_log, period_days: int, now: datetime) -> str:
    """Helper to format a human-readable time-remaining string."""
    if oldest_log:
        last_time = oldest_log["timestamp"]
        last_time_utc = last_time.replace(tzinfo=timezone.utc)
        next_available = last_time_utc + timedelta(days=period_days)
        time_left = next_available - now
        hours_left = int(time_left.total_seconds() / 3600)
        if hours_left >= 24:
            return f"{int(hours_left / 24)} day(s)"
        elif hours_left > 0:
            return f"{hours_left} hour(s)"
        else:
            minutes_left = max(1, int(time_left.total_seconds() / 60))
            return f"{minutes_left} minute(s)"
    return f"{period_days} day(s)"


async def record_usage(user_id: str, endpoint_id: str, tokens_used: int = 0) -> None:
    """
    Records a successful call to the given endpoint.
    For token-based endpoints, pass tokens_used to track consumption.
    """
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        doc = {
            "user_id": str(user_id),
            "endpoint_id": endpoint_id,
            "timestamp": now,
        }
        if tokens_used > 0:
            doc["tokens_used"] = tokens_used
        await _usage_logs_col().insert_one(doc)
        token_info = f", tokens={tokens_used}" if tokens_used else ""
        logger.info(f"[USAGE] Recorded call to {endpoint_id} for user {user_id}{token_info}")
    except Exception as e:
        logger.error(f"Failed to record usage for {user_id} on {endpoint_id}: {e}")


def estimate_tokens(text: str) -> int:
    """
    Rough token estimation: ~4 characters per token for English text.
    This is a reasonable approximation for Claude/GPT models.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)
