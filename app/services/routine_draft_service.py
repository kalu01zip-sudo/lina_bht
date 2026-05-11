import uuid
from datetime import datetime

from app.core.mongo_client import (
    db
)


collection = db[
    "generated_routine_drafts"
]


# ==========================================
# HELPERS
# ==========================================

def new_routine_id():

    return str(
        uuid.uuid4()
    )


def _normalize_weekly_key(
    key: str
):

    if key == "weekly_care":

        return "weekly"

    return key


def _draft_row(

    user_id: str,

    source: str,

    routine_id: str,

    time: str,

    phase: str,

    product_category: str,

    product_name: str,

    product_url: str,

    why: str,

    scan_id: str = None
):

    return {
        "routine_id": routine_id,
        "user_id": user_id,
        "source": source,
        "scan_id": scan_id,
        "time": time,
        "phase": phase,
        "product_category": product_category,
        "product_name": product_name,
        "product_url": product_url,
        "why": why,
        "created_at": datetime.utcnow()
    }


def _save_drafts(
    rows: list
):

    for row in rows:

        collection.update_one(
            {
                "routine_id": row["routine_id"],
                "user_id": row["user_id"]
            },
            {
                "$set": row
            },
            upsert=True
        )

    return rows


# ==========================================
# REGISTER GROUPED ROUTINE RESPONSE
# ==========================================

def register_grouped_routine_drafts(

    user_id: str,

    source: str,

    routine: dict,

    scan_id: str = None
):

    rows = []

    for key in (
        "morning",
        "night",
        "weekly",
        "weekly_care"
    ):

        steps = routine.get(
            key,
            []
        )

        time = _normalize_weekly_key(
            key
        )

        for step in steps:

            routine_id = step.get(
                "id"
            ) or new_routine_id()

            step["id"] = routine_id

            rows.append(
                _draft_row(
                    user_id=user_id,
                    source=source,
                    routine_id=routine_id,
                    time=time,
                    phase=step.get(
                        "phase",
                        "maintenance"
                    ),
                    product_category=step.get(
                        "product_category"
                    ),
                    product_name=step.get(
                        "product_name"
                    ),
                    product_url=step.get(
                        "product_url"
                    ),
                    why=step.get(
                        "why"
                    ) or step.get(
                        "usage_reason"
                    ),
                    scan_id=scan_id
                )
            )

    return _save_drafts(
        rows
    )


# ==========================================
# REGISTER PRODUCT ROUTINE RESPONSE
# ==========================================

def register_product_routine_drafts(

    user_id: str,

    source: str,

    routine_data: dict,

    scan_id: str = None
):

    routine = routine_data.get(
        "routine",
        {}
    )

    rows = []

    for step in routine.get(
        "steps",
        []
    ):

        routine_id = step.get(
            "id"
        ) or new_routine_id()

        step["id"] = routine_id

        rows.append(
            _draft_row(
                user_id=user_id,
                source=source,
                routine_id=routine_id,
                time=routine.get(
                    "time"
                ),
                phase=step.get(
                    "phase",
                    routine.get(
                        "phase",
                        "maintenance"
                    )
                ),
                product_category=step.get(
                    "product_category"
                ),
                product_name=step.get(
                    "product_name"
                ),
                product_url=step.get(
                    "product_url"
                ),
                why=step.get(
                    "usage_reason"
                ) or step.get(
                    "why"
                ),
                scan_id=scan_id
            )
        )

    return _save_drafts(
        rows
    )


# ==========================================
# FETCH FOR SAVE
# ==========================================

def get_routine_drafts_by_ids(

    user_id: str,

    routine_ids: list
):

    docs = list(
        collection.find({
            "user_id": user_id,
            "routine_id": {
                "$in": routine_ids
            }
        })
    )

    by_id = {
        doc["routine_id"]: doc
        for doc in docs
    }

    return [
        by_id[routine_id]
        for routine_id in routine_ids
        if routine_id in by_id
    ]


def missing_routine_ids(

    routine_ids: list,

    drafts: list
):

    found = {
        draft["routine_id"]
        for draft in drafts
    }

    return [
        routine_id
        for routine_id in routine_ids
        if routine_id not in found
    ]
