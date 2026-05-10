from datetime import datetime, timedelta

from app.core.mongo_client import db
from bson import ObjectId


# ==========================================
# COLLECTION
# ==========================================

product_scan_col = db["product_scan_history"]


# ==========================================
# SAVE PRODUCT SCAN
# ==========================================

def save_product_scan(

    user_id: str,

    scan_data: dict
):

    payload = {

        "user_id": user_id,

        "created_at": datetime.utcnow(),

        **scan_data
    }

    result = product_scan_col.insert_one(
        payload
    )

    return str(result.inserted_id)


# ==========================================
# GET LAST 2 MONTHS PRODUCT HISTORY
# ==========================================

def get_recent_product_scans(

    user_id: str,

    months: int = 2
):

    since = datetime.utcnow() - timedelta(
        days=30 * months
    )

    scans = product_scan_col.find({

        "user_id": user_id,

        "created_at": {
            "$gte": since
        }

    }).sort(
        "created_at",
        -1
    )

    result = []

    for item in scans:

        item["_id"] = str(item["_id"])

        result.append(item)

    return result


# ==========================================
# GET PRODUCT SCAN BY ID
# ==========================================

def get_product_scan_by_id(
    scan_id: str
):

    if not ObjectId.is_valid(scan_id):
        return None

    scan = product_scan_col.find_one({

        "_id": ObjectId(scan_id)
    })

    if not scan:
        return None

    scan["_id"] = str(scan["_id"])

    return scan
