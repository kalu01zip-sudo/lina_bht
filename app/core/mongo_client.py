from pymongo import MongoClient
import os
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


def fetch_all_detected_conditions() -> list[str]:
    conditions = set()
    try:
        # Get distinct from nutritions
        for c in nutritions_collection.distinct("detected_condition"):
            if c:
                if isinstance(c, list):
                    for item in c:
                        conditions.add(item.strip().lower().replace(" ", "_"))
                else:
                    conditions.add(c.strip().lower().replace(" ", "_"))
        # Get distinct from foods
        for c in foods_collection.distinct("detected_condition"):
            if c:
                if isinstance(c, list):
                    for item in c:
                        conditions.add(item.strip().lower().replace(" ", "_"))
                else:
                    conditions.add(c.strip().lower().replace(" ", "_"))
        # Get distinct from recipes
        for c in recipes_collection.distinct("detected_condition"):
            if c:
                if isinstance(c, list):
                    for item in c:
                        conditions.add(item.strip().lower().replace(" ", "_"))
                else:
                    conditions.add(c.strip().lower().replace(" ", "_"))
    except Exception as e:
        print("[ERROR] Failed to fetch unique conditions from collections:", e)
    
    # Fallback to standard conditions if collections are empty/failed
    if not conditions:
        conditions = {
            "acne", "blackheads", "whiteheads", "pores", "oiliness", "dryness", 
            "dehydration", "redness", "irritation", "sensitivity", "pigmentation", 
            "dark_spots", "uneven_tone", "dullness", "dark_circles", "eye_bags", 
            "fine_lines", "wrinkles", "loss_of_elasticity", "sun_damage",
            "dandruff", "oily_scalp", "dry_scalp", "inflammation", "product_buildup",
            "hair_thinning", "hair_loss", "split_ends", "brittle_hair", "scalp_acne"
        }
    return sorted(list(conditions))


