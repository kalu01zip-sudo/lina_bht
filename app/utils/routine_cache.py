"""
app/utils/routine_cache.py
──────────────────────────
Lightweight in-memory TTL cache for AI-generated routines.

Usage:
    from app.utils.routine_cache import get_cached_routine, set_cached_routine

    cached = get_cached_routine("face", user_id, scan_id)
    if cached:
        return cached

    result = await generate_routine(...)
    set_cached_routine("face", user_id, scan_id, result)
    return result

Notes:
- TTL defaults to 60 minutes (ROUTINE_CACHE_TTL_SECONDS).
- Safe for single-worker deployments. For multi-worker (--workers N > 1),
  switch to a MongoDB or Redis cache instead.
- Thread-safe: reads/writes on a plain dict are GIL-protected in CPython.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ROUTINE_CACHE_TTL_SECONDS: int = 60 * 60  # 1 hour

# ── Internal store: key → (stored_at_unix_ts, data) ──────────────────────────
_cache: dict[str, tuple[float, Any]] = {}


# ── Public API ────────────────────────────────────────────────────────────────

def _make_key(endpoint: str, *parts: str) -> str:
    """Build a namespaced cache key from endpoint + identifiers."""
    return ":".join([endpoint, *parts])


def get_cached_routine(endpoint: str, *key_parts: str) -> Any | None:
    """
    Return the cached value for this endpoint + key parts, or None if
    missing / expired.

    Args:
        endpoint:  Short label, e.g. "face", "scalp", "manual_ai"
        key_parts: Variable identifiers (user_id, scan_id, etc.)
    """
    key = _make_key(endpoint, *key_parts)
    entry = _cache.get(key)
    if entry is None:
        return None

    stored_at, data = entry
    if time.time() - stored_at > ROUTINE_CACHE_TTL_SECONDS:
        del _cache[key]
        logger.debug("Routine cache expired for key=%s", key)
        return None

    logger.info("Routine cache HIT for key=%s", key)
    return data


def set_cached_routine(endpoint: str, *key_parts_and_value) -> None:
    """
    Store a value in the cache.

    Call as:  set_cached_routine("face", user_id, scan_id, result_dict)
    Last argument is always the value; everything before it is the key.
    """
    *key_parts, value = key_parts_and_value
    key = _make_key(endpoint, *[str(p) for p in key_parts])
    _cache[key] = (time.time(), value)
    logger.info("Routine cache SET for key=%s", key)


def invalidate_routine_cache(endpoint: str, *key_parts: str) -> None:
    """Manually evict a specific cache entry (e.g. after the user edits a scan)."""
    key = _make_key(endpoint, *key_parts)
    _cache.pop(key, None)
    logger.debug("Routine cache invalidated for key=%s", key)


def cache_stats() -> dict[str, int]:
    """Return current cache size and live (non-expired) entry count."""
    now = time.time()
    live = sum(
        1 for _, (ts, _) in _cache.items()
        if now - ts <= ROUTINE_CACHE_TTL_SECONDS
    )
    return {"total_entries": len(_cache), "live_entries": live}
