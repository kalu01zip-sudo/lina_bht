# app/services/onesignal.py
"""
OneSignal Push Notification Service.
Sends notifications via OneSignal's REST API.
"""

import os
import logging
from typing import Optional, List, Tuple
import httpx

logger = logging.getLogger(__name__)

ONESIGNAL_APP_ID = os.getenv("ONESIGNAL_APP_ID", "")
ONESIGNAL_REST_API_KEY = os.getenv("ONESIGNAL_REST_API_KEY", "")


def send_push_to_user_subscriptions(
    subscription_ids: List[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> Tuple[int, List[str]]:
    """
    Send push notification to OneSignal subscription IDs.
    Returns (success_count, list_of_failed_subscriptions).
    """
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        logger.warning(
            "OneSignal not configured — ONESIGNAL_APP_ID or "
            "ONESIGNAL_REST_API_KEY not set. Push notifications disabled."
        )
        return 0, []

    if not subscription_ids:
        return 0, []

    url = "https://onesignal.com/api/v1/notifications"
    headers = {
        "Authorization": f"Basic {ONESIGNAL_REST_API_KEY}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "app_id": ONESIGNAL_APP_ID,
        "include_subscription_ids": subscription_ids,
        "headings": {"en": title},
        "contents": {"en": body},
        "data": {k: str(v) for k, v in (data or {}).items()},
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            res_data = response.json()

            logger.info("OneSignal push sent response: %s", res_data)
            
            # Try to identify invalid/expired subscriptions from OneSignal response
            invalid_ids = []
            errors = res_data.get("errors", [])
            warnings = res_data.get("warnings", [])

            # Check inside errors/warnings for invalid_player_ids / invalid_subscription_ids
            for field in [errors, warnings]:
                if isinstance(field, dict):
                    invalid_ids.extend(field.get("invalid_player_ids", []))
                    invalid_ids.extend(field.get("invalid_subscription_ids", []))
                elif isinstance(field, list):
                    # Sometimes errors is a list of strings
                    pass

            failed = [fid for fid in invalid_ids if fid in subscription_ids]
            success_count = max(0, len(subscription_ids) - len(failed))

            return success_count, failed

    except httpx.HTTPStatusError as exc:
        logger.error("OneSignal HTTP error: %s - Response: %s", exc, exc.response.text)
        # Attempt to parse invalid ids from the error response
        try:
            err_data = exc.response.json()
            errs = err_data.get("errors", [])
            if isinstance(errs, dict):
                invalid_ids = errs.get("invalid_player_ids", [])
                invalid_ids.extend(errs.get("invalid_subscription_ids", []))
                failed = [fid for fid in invalid_ids if fid in subscription_ids]
                return len(subscription_ids) - len(failed), failed
        except Exception:
            pass
        return 0, subscription_ids
    except Exception as exc:
        logger.error("OneSignal push request failed: %s", exc)
        return 0, subscription_ids
