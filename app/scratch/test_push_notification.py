# app/scratch/test_push_notification.py
"""
Utility script to test and diagnose Push Notifications.
Usage:
  python app/scratch/test_push_notification.py --email <user-email>
  python app/scratch/test_push_notification.py --sub-id <onesignal-subscription-id>
"""

import sys
import os
import argparse
import logging
from dotenv import load_dotenv

# Set logging level to INFO so we see OneSignal responses
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# Load env variables
load_dotenv()

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.services.onesignal import send_push_to_user_subscriptions, ONESIGNAL_APP_ID, ONESIGNAL_REST_API_KEY

def diagnose_config():
    print("=" * 60)
    print("DIAGNOSING PUSH NOTIFICATION CONFIGURATION")
    print("=" * 60)
    print(f"ONESIGNAL_APP_ID: {ONESIGNAL_APP_ID or '❌ NOT SET'}")
    
    if ONESIGNAL_REST_API_KEY:
        masked_key = ONESIGNAL_REST_API_KEY[:6] + "..." + ONESIGNAL_REST_API_KEY[-6:] if len(ONESIGNAL_REST_API_KEY) > 12 else "***"
        print(f"ONESIGNAL_REST_API_KEY: {masked_key}")
    else:
        print("ONESIGNAL_REST_API_KEY: ❌ NOT SET")
        
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        print("\n[ERROR] OneSignal is not fully configured in your .env file!")
        print("Please ensure both ONESIGNAL_APP_ID and ONESIGNAL_REST_API_KEY are correct.")
        return False
    
    print("\n[OK] Environment variables are set. Checking database connectivity...")
    return True

def get_user_by_email(email: str):
    from app.core.database import get_db
    import asyncio

    async def _fetch():
        db = get_db()
        user = await db.users.find_one({"email": email})
        return user

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_fetch())

def main():
    parser = argparse.ArgumentParser(description="Test OneSignal push notifications.")
    parser.add_argument("--email", help="Email of the user to test sending to.")
    parser.add_argument("--sub-id", help="Direct OneSignal Subscription ID (Player ID) to send to.")
    parser.add_argument("--title", default="Test Push Notification", help="Notification title.")
    parser.add_argument("--body", default="Hello from SkinSense! Your push notifications are working.", help="Notification body.")
    
    args = parser.parse_args()
    
    if not diagnose_config():
        sys.exit(1)
        
    subscription_ids = []
    
    if args.sub_id:
        subscription_ids = [args.sub_id.strip()]
        print(f"\nUsing direct Subscription ID: {subscription_ids[0]}")
    elif args.email:
        print(f"\nLooking up user by email: {args.email}")
        user = get_user_by_email(args.email)
        if not user:
            print(f"[ERROR] User with email '{args.email}' not found in MongoDB.")
            sys.exit(1)
            
        print(f"User found: {user.get('full_name')} (ID: {user.get('_id')})")
        subscription_ids = user.get("onesignal_subscriptions", [])
        print(f"Registered subscriptions (devices): {subscription_ids}")
        
        if not subscription_ids:
            print("\n[WARNING] This user has NO registered OneSignal Subscription IDs in MongoDB!")
            print("To receive push notifications, the mobile app must call POST /auth/onesignal-subscription")
            print("after the user logs in or registers on their device.")
            sys.exit(1)
    else:
        print("\n[USAGE] Please provide either --email or --sub-id to trigger a test push.")
        print("Example:")
        print("  python app/scratch/test_push_notification.py --email test@example.com")
        print("  python app/scratch/test_push_notification.py --sub-id 12345678-abcd-1234-abcd-123456789abc")
        sys.exit(0)

    print(f"\nSending test notification...")
    print(f"Title: {args.title}")
    print(f"Body:  {args.body}")
    
    success_count, failed_ids = send_push_to_user_subscriptions(
        subscription_ids=subscription_ids,
        title=args.title,
        body=args.body,
        data={"test": "true"}
    )
    
    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)
    print(f"Attempted: {len(subscription_ids)}")
    print(f"Succeeded: {success_count}")
    print(f"Failed:    {len(failed_ids)}")
    if failed_ids:
        print(f"Failed subscription IDs (either expired, invalid, or unsubscribed):")
        for fid in failed_ids:
            print(f"  - {fid}")
            
    if success_count > 0:
        print("\n[SUCCESS] Test push notification sent successfully via OneSignal!")
    else:
        print("\n[FAILURE] No push notifications were successfully delivered.")
        print("Check OneSignal dashboard/logs for error details or verify if subscription IDs are still active.")

if __name__ == "__main__":
    main()
