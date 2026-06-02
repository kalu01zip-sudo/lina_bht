import os
import sys
from dotenv import load_dotenv

# Ensure we can import app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

load_dotenv()

from app.core.mongo_client import nutritions_collection

def migrate():
    print("Starting migration of 'detected conditions' -> 'detected_condition' in nutritions collection...")
    
    # Update all documents that have "detected conditions" field
    cursor = nutritions_collection.find({"detected conditions": {"$exists": True}})
    count = 0
    for doc in cursor:
        doc_id = doc["_id"]
        conds = doc.get("detected conditions")
        
        # Set the new field and unset the old field
        nutritions_collection.update_one(
            {"_id": doc_id},
            {
                "$set": {"detected_condition": conds},
                "$unset": {"detected conditions": ""}
            }
        )
        count += 1
        print(f"Updated document ID: {doc_id}, set detected_condition to: {conds}")
        
    print(f"Migration completed. Total documents updated: {count}")

if __name__ == "__main__":
    migrate()
