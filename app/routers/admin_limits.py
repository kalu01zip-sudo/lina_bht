# app/routers/admin_limits.py

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Any, Optional

from app.routers.admin_auth import _get_current_admin
from app.services.usage_limiter import _usage_limits_col, DEFAULT_LIMITS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/limits", tags=["Admin Limits"])
CurrentAdmin = Depends(_get_current_admin)


class UpdateCountLimitRequest(BaseModel):
    """For count-based endpoints (face_scan, scalp_scan)."""
    free_max_uses: int = Field(..., ge=0, description="Max uses per period for Free tier. 0 = unlimited.")
    free_period_days: int = Field(..., ge=1, description="Period in days for Free tier.")
    premium_max_uses: int = Field(..., ge=0, description="Max uses per period for Premium tier. 0 = unlimited.")
    premium_period_days: int = Field(..., ge=1, description="Period in days for Premium tier.")


class UpdateTokenLimitRequest(BaseModel):
    """For token-based endpoints (lia_chat)."""
    free_token_budget: int = Field(..., ge=0, description="Token budget for Free tier. 0 = unlimited.")
    free_period_days: int = Field(..., ge=1, description="Period in days for Free tier.")
    premium_token_budget: int = Field(..., ge=0, description="Token budget for Premium tier. 0 = unlimited.")
    premium_period_days: int = Field(..., ge=1, description="Period in days for Premium tier.")


class UpdateLimitRequest(BaseModel):
    """
    Unified update request. Send the fields relevant to the endpoint type.
    Count-based fields for face_scan/scalp_scan, token-based for lia_chat.
    """
    # Count-based fields
    free_max_uses: Optional[int] = Field(None, ge=0)
    free_period_days: Optional[int] = Field(None, ge=1)
    premium_max_uses: Optional[int] = Field(None, ge=0)
    premium_period_days: Optional[int] = Field(None, ge=1)

    # Token-based fields
    free_token_budget: Optional[int] = Field(None, ge=0)
    premium_token_budget: Optional[int] = Field(None, ge=0)


@router.get("")
async def get_all_limits(_: Any = CurrentAdmin):
    """
    Get the configured usage limits for all rate-limited endpoints.
    Merges database overrides with default settings.
    """
    try:
        # Load all overrides from DB
        db_configs = {}
        cursor = _usage_limits_col().find({})
        async for doc in cursor:
            db_configs[doc["_id"]] = {k: v for k, v in doc.items() if k != "_id"}

        # Merge with defaults
        result = []
        for endpoint_id, defaults in DEFAULT_LIMITS.items():
            db_override = db_configs.get(endpoint_id, {})
            limit_type = defaults.get("type", "count")

            entry = {
                "endpoint_id": endpoint_id,
                "type": limit_type,
                "is_default": endpoint_id not in db_configs,
            }

            if limit_type == "count":
                entry["free_max_uses"] = db_override.get("free_max_uses", defaults.get("free_max_uses", 1))
                entry["free_period_days"] = db_override.get("free_period_days", defaults.get("free_period_days", 7))
                entry["premium_max_uses"] = db_override.get("premium_max_uses", defaults.get("premium_max_uses", 1))
                entry["premium_period_days"] = db_override.get("premium_period_days", defaults.get("premium_period_days", 1))
            elif limit_type == "token":
                entry["free_token_budget"] = db_override.get("free_token_budget", defaults.get("free_token_budget", 10000))
                entry["free_period_days"] = db_override.get("free_period_days", defaults.get("free_period_days", 7))
                entry["premium_token_budget"] = db_override.get("premium_token_budget", defaults.get("premium_token_budget", 100000))
                entry["premium_period_days"] = db_override.get("premium_period_days", defaults.get("premium_period_days", 1))

            result.append(entry)

        return {
            "success": True,
            "limits": result
        }
    except Exception as e:
        logger.error(f"Error fetching limits config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve limits configuration."
        )


@router.put("/{endpoint_id}")
async def update_endpoint_limit(
    endpoint_id: str,
    payload: UpdateLimitRequest,
    _: Any = CurrentAdmin
):
    """
    Create or update usage limits for a specific endpoint.
    Admin authorization required.

    For count-based endpoints (face_scan, scalp_scan), send:
      free_max_uses, free_period_days, premium_max_uses, premium_period_days

    For token-based endpoints (lia_chat), send:
      free_token_budget, free_period_days, premium_token_budget, premium_period_days
    """
    if endpoint_id not in DEFAULT_LIMITS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid endpoint ID. Allowed: {list(DEFAULT_LIMITS.keys())}"
        )

    defaults = DEFAULT_LIMITS[endpoint_id]
    limit_type = defaults.get("type", "count")

    # Build the $set document from provided fields
    update_doc = {}

    if limit_type == "count":
        if payload.free_max_uses is not None:
            update_doc["free_max_uses"] = payload.free_max_uses
        if payload.free_period_days is not None:
            update_doc["free_period_days"] = payload.free_period_days
        if payload.premium_max_uses is not None:
            update_doc["premium_max_uses"] = payload.premium_max_uses
        if payload.premium_period_days is not None:
            update_doc["premium_period_days"] = payload.premium_period_days
    elif limit_type == "token":
        if payload.free_token_budget is not None:
            update_doc["free_token_budget"] = payload.free_token_budget
        if payload.free_period_days is not None:
            update_doc["free_period_days"] = payload.free_period_days
        if payload.premium_token_budget is not None:
            update_doc["premium_token_budget"] = payload.premium_token_budget
        if payload.premium_period_days is not None:
            update_doc["premium_period_days"] = payload.premium_period_days

    if not update_doc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No valid fields provided for {limit_type}-based endpoint."
        )

    # Always store the type
    update_doc["type"] = limit_type

    try:
        await _usage_limits_col().update_one(
            {"_id": endpoint_id},
            {"$set": update_doc},
            upsert=True
        )

        logger.info(f"[ADMIN] Updated limits for {endpoint_id}: {update_doc}")

        return {
            "success": True,
            "message": f"Limits for {endpoint_id} updated successfully.",
            "endpoint_id": endpoint_id,
            **update_doc
        }
    except Exception as e:
        logger.error(f"Failed to update limits for {endpoint_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save limits configuration."
        )


@router.get("/{endpoint_id}/usage")
async def get_endpoint_usage_stats(
    endpoint_id: str,
    _: Any = CurrentAdmin
):
    """
    Get aggregated usage statistics for an endpoint.
    Useful for admin dashboard monitoring.
    """
    if endpoint_id not in DEFAULT_LIMITS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid endpoint ID. Allowed: {list(DEFAULT_LIMITS.keys())}"
        )

    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        last_24h = now - timedelta(hours=24)
        last_7d = now - timedelta(days=7)

        # Count calls in last 24h and 7d
        
        pipeline_24h = [
            {"$match": {"endpoint_id": endpoint_id, "timestamp": {"$gte": last_24h}}},
            {"$group": {
                "_id": None,
                "total_calls": {"$sum": 1},
                "total_tokens": {"$sum": {"$ifNull": ["$tokens_used", 0]}},
                "unique_users": {"$addToSet": "$user_id"}
            }}
        ]
        pipeline_7d = [
            {"$match": {"endpoint_id": endpoint_id, "timestamp": {"$gte": last_7d}}},
            {"$group": {
                "_id": None,
                "total_calls": {"$sum": 1},
                "total_tokens": {"$sum": {"$ifNull": ["$tokens_used", 0]}},
                "unique_users": {"$addToSet": "$user_id"}
            }}
        ]

        from app.services.usage_limiter import _usage_logs_col
        stats_24h = await _usage_logs_col().aggregate(pipeline_24h).to_list(1)
        stats_7d = await _usage_logs_col().aggregate(pipeline_7d).to_list(1)

        def _format_stats(raw):
            if not raw:
                return {"total_calls": 0, "total_tokens": 0, "unique_users": 0}
            return {
                "total_calls": raw[0].get("total_calls", 0),
                "total_tokens": raw[0].get("total_tokens", 0),
                "unique_users": len(raw[0].get("unique_users", [])),
            }

        return {
            "success": True,
            "endpoint_id": endpoint_id,
            "last_24h": _format_stats(stats_24h),
            "last_7d": _format_stats(stats_7d),
        }
    except Exception as e:
        logger.error(f"Failed to get usage stats for {endpoint_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve usage statistics."
        )
