# database.py
"""
MongoDB async connection using Motor.
Collections:
  users          → mobile app user accounts
  admins         → admin dashboard accounts (separate from users)
  otp_codes      → email OTP for verify + reset (shared by users and admins)
  refresh_tokens → JWT refresh token store (shared by users and admins)
  products       → admin product database (skincare/haircare products)
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME   = os.environ.get("DB_NAME",   "skinsense")

_client: AsyncIOMotorClient = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            MONGO_URL,
            serverSelectionTimeoutMS=5000,
        )
    return _client


def get_db():
    return get_client()[DB_NAME]


# Collection shortcuts
def users_col():
    return get_db()["users"]

def otp_col():
    return get_db()["otp_codes"]

def tokens_col():
    return get_db()["refresh_tokens"]

def subscriptions_col():
    return get_db()["subscriptions"]

def admins_col():
    return get_db()["admins"]

def products_col():
    return get_db()["products"]

def plan_config_col():
    return get_db()["plan_config"]

def nutritions_col():
    return get_db()["nutritions"]

def foods_col():
    return get_db()["foods"]

def recipes_col():
    return get_db()["recipes"]

def saved_routines_col():
    return get_db()["saved_routines"]

def routine_videos_col():
    return get_db()["routine_videos"]

def lia_notifications_col():
    return get_db()["lia_notifications"]

def admin_notification_settings_col():
    return get_db()["admin_notification_settings"]

def legal_contents_col():
    return get_db()["legal_contents"]

def support_tickets_col():
    return get_db()["support_tickets"]



async def create_indexes():
    """Create MongoDB indexes on startup — safe to run multiple times."""
    try:
        db = get_db()

        # users
        await db.users.create_index("email",     unique=True)
        await db.users.create_index("google_id", sparse=True)

        # otp_codes — auto-expire via TTL index (MongoDB deletes after expiry)
        await db.otp_codes.create_index("email")
        await db.otp_codes.create_index(
            "expires_at",
            expireAfterSeconds=0    # MongoDB auto-deletes when expires_at is past
        )

        # refresh_tokens — auto-expire
        await db.refresh_tokens.create_index("token",   unique=True)
        await db.refresh_tokens.create_index("user_id")
        await db.refresh_tokens.create_index(
            "expires_at",
            expireAfterSeconds=0
        )

        # Apple Sign-In
        await db.users.create_index("apple_id", sparse=True)

        # RevenueCat Subscriptions
        await db.subscriptions.create_index("user_id")
        await db.subscriptions.create_index("rc_app_user_id", unique=True, sparse=True)
        await db.subscriptions.create_index("rc_entitlement_id")
        await db.chat_messages.create_index("user_id")
        await db.chat_messages.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

        # Routine steps
        await db.routine_steps.create_index("user_id")
        await db.routine_steps.create_index([("user_id", ASCENDING), ("time_slot", ASCENDING)])
        await db.routine_steps.create_index([("user_id", ASCENDING), ("time_slot", ASCENDING), ("order", ASCENDING)])

        # Scan results — face & hair/scalp history + comparison queries
        await db.scan_results.create_index("user_id")
        await db.scan_results.create_index([("user_id", ASCENDING), ("scan_type", ASCENDING)])
        await db.scan_results.create_index([("user_id", ASCENDING), ("scan_type", ASCENDING), ("scanned_at", DESCENDING)])

        # Admin accounts — unique email index
        await db.admins.create_index("email", unique=True)

        # ── Products collection (admin product database) ───────────────────────────
        # Fast lookup by barcode (unique, sparse — allows multiple null barcodes)
        await db.products.create_index("barcode", unique=True, sparse=True)
        # Text + case-insensitive search on name and brand
        await db.products.create_index([("name", ASCENDING)])
        await db.products.create_index([("brand", ASCENDING)])
        # Filter by status (active/inactive)
        await db.products.create_index("status")
        # Filter products with no image — compound for fast admin queries
        await db.products.create_index([("image_url", ASCENDING), ("status", ASCENDING)])

        # ── plan_config collection (admin-editable plan limits & pricing) ────────────
        # _id is "basic" or "premium" — already unique as the primary key.
        # Index on plan_type for any future multi-tier lookups.
        await db.plan_config.create_index("plan_type", unique=True, sparse=True)

        # ── webhook_events (subscription webhook idempotency) ────────────────────────
        # Auto-delete processed event records after 7 days to keep collection small.
        await db.webhook_events.create_index(
            "processed_at",
            expireAfterSeconds=7 * 24 * 3600,   # 7 days TTL
        )

        # ── Migrated collections from Supabase ───────────────────────────────────────
        await db.nutritions.create_index("id", unique=True)
        await db.foods.create_index("id", unique=True)
        await db.recipes.create_index("id", unique=True)
        await db.saved_routines.create_index("id", unique=True, sparse=True)
        await db.saved_routines.create_index("user_id")
        await db.routine_videos.create_index([("phase", ASCENDING), ("product_category", ASCENDING)])
        await db.lia_notifications.create_index("user_id")
        await db.lia_notifications.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await db.lia_notifications.create_index([("trigger", ASCENDING), ("created_at", DESCENDING)])
        await db.admin_notification_settings.create_index("updated_at")
        await db.legal_contents.create_index("id", unique=True)

        print("[OK] MongoDB indexes created.")
    except Exception as e:
        print(f"[WARN] MongoDB unavailable for index creation (non-fatal): {str(e)[:100]}")
        print("   Ensure MongoDB is running on localhost:27017")
        print("   Application will continue but DB operations may fail.")
