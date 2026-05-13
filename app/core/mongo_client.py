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
