from pymongo import MongoClient
import os
import time
from dotenv import load_dotenv

load_dotenv()

mongo_url = os.getenv("MONGO_URL")
db_name = os.getenv("DB_NAME", "skinsense")

if not mongo_url:
    raise ValueError("MongoDB env not loaded: set MONGO_URL or MONGO_URI")

client = MongoClient(
    mongo_url,
    serverSelectionTimeoutMS=5000
)

db = client[db_name]
scan_collection = db["face_scans"]
scalp_scan_collection = db["scalp_scans"]
routine_detail_collection = db["routine_details"]

# Migrated collections from Supabase
nutritions_collection = db["nutritions"]
foods_collection = db["foods"]
recipes_collection = db["recipes"]
products_collection = db["products"]
saved_routines_collection = db["saved_routines"]
routine_videos_collection = db["routine_videos"]
lia_notifications_collection = db["lia_notifications"]
legal_contents_collection = db["legal_contents"]
support_tickets_collection = db["support_tickets"]


# ─────────────────────────────────────────────────────────────────────────────
# TTL cache for detected conditions
# fetch_all_detected_conditions() does 3 MongoDB distinct() calls.
# Without caching it runs on every face/scalp scan request.
# With a 5-minute cache, cold-path only runs once per 300 s per process.
# ─────────────────────────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS: float = 300.0   # 5 minutes
_conditions_cache: list[str] = []
_conditions_cache_ts: float = 0.0


def bust_conditions_cache() -> None:
    """
    Invalidate the in-memory conditions cache immediately.
    Call this after an admin adds a new condition tag to any collection.
    """
    global _conditions_cache_ts
    _conditions_cache_ts = 0.0


def _normalise_condition(raw: str) -> str:
    """Lowercase, strip whitespace, replace spaces/hyphens with underscores."""
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


def fetch_all_detected_conditions() -> list[str]:
    """
    Returns a sorted list of every unique detected_condition tag found across
    the nutritions, foods, and recipes collections.

    Result is cached for _CACHE_TTL_SECONDS (default 5 min) to avoid
    repeated MongoDB round-trips on every scan request.

    Falls back to a hardcoded list if the DB is unreachable or empty.
    """
    global _conditions_cache, _conditions_cache_ts

    # ── Serve from cache if still fresh ──────────────────────────────────────
    if _conditions_cache and (time.monotonic() - _conditions_cache_ts) < _CACHE_TTL_SECONDS:
        return _conditions_cache

    # ── Re-fetch from MongoDB ─────────────────────────────────────────────────
    conditions: set[str] = set()
    try:
        for collection in (nutritions_collection, foods_collection, recipes_collection):
            for c in collection.distinct("detected_condition"):
                if not c:
                    continue
                items = c if isinstance(c, list) else [c]
                for item in items:
                    if isinstance(item, str) and item.strip():
                        conditions.add(_normalise_condition(item))
    except Exception as exc:
        print(f"[WARN] fetch_all_detected_conditions DB error (using fallback): {exc}")

    # ── Fallback if DB is empty or unavailable ────────────────────────────────
    if not conditions:
        conditions = {
            # ── Face / skin conditions ──────────────────────────────────────
            "acne", "blackheads", "whiteheads", "pores", "oiliness", "dryness",
            "dehydration", "redness", "irritation", "sensitivity", "pigmentation",
            "dark_spots", "uneven_tone", "dullness", "dark_circles", "eye_bags",
            "fine_lines", "wrinkles", "loss_of_elasticity", "sun_damage",
            # Extended face conditions
            "hyperpigmentation", "melasma", "rosacea", "eczema", "psoriasis",
            "contact_dermatitis", "perioral_dermatitis", "milia", "syringoma",
            "sebaceous_filaments",
            # ── Scalp / hair conditions ─────────────────────────────────────
            "dandruff", "oily_scalp", "dry_scalp", "inflammation", "product_buildup",
            "hair_thinning", "hair_loss", "split_ends", "brittle_hair", "scalp_acne",
            "folliculitis", "seborrheic_dermatitis", "alopecia",
            "telogen_effluvium", "androgenetic_alopecia",
            "scalp_fungus", "scalp_irritation",
            "hair_breakage", "sebum_overproduction", "hair_color_damage",
            "heat_damage", "chemical_damage",
        }

    result = sorted(conditions)

    # ── Store in cache ────────────────────────────────────────────────────────
    _conditions_cache = result
    _conditions_cache_ts = time.monotonic()
    return result
