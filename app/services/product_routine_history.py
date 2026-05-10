from datetime import datetime

from app.core.mongo_client import db
from bson import ObjectId


collection = db[
    "generated_product_routines"
]


# ==========================================
# GET EXISTING ROUTINE
# ==========================================

def get_existing_product_routine(

    user_id: str,

    product_scan_id: str
):

    return collection.find_one({

        "user_id": user_id,

        "product_scan_id":
            product_scan_id
    })


# ==========================================
# SAVE GENERATED ROUTINE
# ==========================================

def save_generated_product_routine(

    user_id: str,

    product_scan_id: str,

    routine_data: dict
):

    payload = {

        "user_id": user_id,

        "product_scan_id":
            product_scan_id,

        "routine": routine_data,

        "created_at":
            datetime.utcnow()
    }

    result = collection.insert_one(
        payload
    )

    return str(result.inserted_id)