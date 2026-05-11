# app/services/firebase_push.py
"""
Firebase Cloud Messaging (FCM) push notification service.

Initializes firebase-admin once at import time.
Gracefully degrades if not configured (logs warning, skips push).
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Firebase initialization ──────────────────────────────────────────────────

_firebase_app = None
_initialized = False


def _init_firebase():
    """Initialize Firebase Admin SDK once. Safe to call multiple times."""
    global _firebase_app, _initialized

    if _initialized:
        return

    _initialized = True

    sa_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")

    if not sa_path or not os.path.exists(sa_path):
        logger.warning(
            "Firebase not configured — FIREBASE_SERVICE_ACCOUNT_PATH "
            "not set or file not found. Push notifications disabled."
        )
        return

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred = credentials.Certificate(sa_path)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialized successfully.")

    except Exception as exc:
        logger.error("Firebase init failed: %s", exc)


# Initialize on import
_init_firebase()


# ── Public API ───────────────────────────────────────────────────────────────

def send_push(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """
    Send a push notification to a single device.
    Returns True on success, False on failure.
    """
    if not _firebase_app:
        logger.debug("Firebase not initialized — skipping push.")
        return False

    try:
        from firebase_admin import messaging

        notification = messaging.Notification(
            title=title,
            body=body,
        )

        message = messaging.Message(
            notification=notification,
            token=fcm_token,
            data={k: str(v) for k, v in (data or {}).items()},
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="lia_coaching",
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    ),
                ),
            ),
        )

        response = messaging.send(message)
        logger.info("FCM push sent: %s", response)
        return True

    except Exception as exc:
        exc_str = str(exc).lower()

        # Token is invalid/expired — caller should remove it
        if "not-registered" in exc_str or "invalid" in exc_str:
            logger.warning(
                "FCM token invalid (will be cleaned): %s",
                fcm_token[:20],
            )
        else:
            logger.error("FCM push failed: %s", exc)

        return False


def send_push_to_user_tokens(
    fcm_tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> tuple[int, list[str]]:
    """
    Send push to all of a user's device tokens.
    Returns (success_count, list_of_failed_tokens).
    """
    if not _firebase_app or not fcm_tokens:
        return 0, []

    success = 0
    failed_tokens = []

    for token in fcm_tokens:
        if send_push(token, title, body, data):
            success += 1
        else:
            failed_tokens.append(token)

    return success, failed_tokens
