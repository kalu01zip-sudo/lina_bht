from datetime import datetime, timezone

def resolve_pregnancy_phase(user: dict | None) -> dict | None:
    """
    Dynamically computes the current pregnancy month based on time elapsed
    since onboarding / registration of pregnancy. Increments +1 month for
    every 30 days, capping at month 10.
    """
    if not user:
        return user

    current_phase = user.get("current_phase")
    start_month = user.get("pregnancy_start_month")
    updated_at = user.get("pregnancy_month_updated_at")

    if current_phase == "pregnant" and start_month is not None and updated_at:
        # Resolve updated_at to datetime object
        dt = updated_at
        if isinstance(dt, str):
            try:
                # Remove Z suffix or handle standard isoformat
                if dt.endswith("Z"):
                    dt = dt[:-1] + "+00:00"
                dt = datetime.fromisoformat(dt)
            except Exception:
                pass
        
        if isinstance(dt, datetime):
            # Ensure comparison is timezone-aware
            now = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            
            delta_days = (now - dt).days
            months_elapsed = max(0, delta_days // 30)
            current_month = min(10, int(start_month) + months_elapsed)
            
            user["life_phase"] = f"pregnant ({current_month})"
            
    return user
