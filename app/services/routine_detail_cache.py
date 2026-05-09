from app.core.mongo_client import routine_detail_collection
from datetime import datetime


def get_cached_routine_detail(routine_id: str):

    return routine_detail_collection.find_one({
        "routine_id": routine_id
    })


def save_cached_routine_detail(
    routine_id: str,
    user_id: str,
    data: dict
):

    routine_detail_collection.insert_one({

        "routine_id": routine_id,

        "user_id": user_id,

        "generated_data": data,

        "created_at": datetime.utcnow()
    })