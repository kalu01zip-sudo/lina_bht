"""
One-time migration: Create the lia_notifications table in Supabase.

Usage:
  1. Run this script: python create_lia_notifications_table.py
  2. Copy the SQL it prints
  3. Paste into Supabase Dashboard > SQL Editor > New Query > Run
  4. Run this script again to verify
"""

import os
from dotenv import load_dotenv

load_dotenv()

SQL = """
CREATE TABLE IF NOT EXISTS lia_notifications (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id     text NOT NULL,
    trigger     text NOT NULL,
    title       text NOT NULL,
    message     text NOT NULL,
    data        jsonb DEFAULT '{}',
    is_read     boolean DEFAULT false,
    created_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lia_notif_user
    ON lia_notifications(user_id);

CREATE INDEX IF NOT EXISTS idx_lia_notif_user_unread
    ON lia_notifications(user_id, is_read);

CREATE INDEX IF NOT EXISTS idx_lia_notif_user_trigger
    ON lia_notifications(user_id, trigger, created_at DESC);

ALTER TABLE lia_notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own notifications"
    ON lia_notifications FOR SELECT
    USING (true);

CREATE POLICY "Service role can insert notifications"
    ON lia_notifications FOR INSERT
    WITH CHECK (true);

CREATE POLICY "Service role can update notifications"
    ON lia_notifications FOR UPDATE
    USING (true);
"""


def verify_table():
    try:
        from app.core.supabase_client import supabase
        supabase.table("lia_notifications").select("id").limit(1).execute()
        return True
    except Exception as e:
        if "PGRST205" in str(e):
            return False
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("  Lia Notifications -- Supabase Table Migration")
    print("=" * 60)
    print()

    if verify_table():
        print("[OK] Table 'lia_notifications' already exists!")
        print("     No action needed.")
    else:
        print("[MISSING] Table 'lia_notifications' does NOT exist yet.")
        print()
        print("Copy the SQL below and run it in:")
        print("   Supabase Dashboard > SQL Editor > New Query > Run")
        print()
        print("-" * 60)
        print(SQL)
        print("-" * 60)
        print()
        print("After running the SQL, re-run this script to verify:")
        print("  python create_lia_notifications_table.py")
