# database.py
"""
MongoDB async connection using Motor.
Collections:
  users          → user accounts
  otp_codes      → email OTP for verify + reset
  refresh_tokens → JWT refresh token store
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
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


async def create_indexes():
    """Create MongoDB indexes on startup — safe to run multiple times."""
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

    # Stripe Subscriptions
    await db.subscriptions.create_index("user_id")
    await db.subscriptions.create_index("stripe_subscription_id", unique=True, sparse=True)
    await db.subscriptions.create_index("stripe_customer_id")
    await db.chat_messages.create_index("user_id")
    await db.chat_messages.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

    # Routine steps
    await db.routine_steps.create_index("user_id")
    await db.routine_steps.create_index([("user_id", ASCENDING), ("time_slot", ASCENDING)])
    await db.routine_steps.create_index([("user_id", ASCENDING), ("time_slot", ASCENDING), ("order", ASCENDING)])

    print("✅ MongoDB indexes created.")