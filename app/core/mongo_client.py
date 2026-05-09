from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"))

db = client["skinsense"]
scan_collection = db["face_scans"]
routine_detail_collection = db["routine_details"]