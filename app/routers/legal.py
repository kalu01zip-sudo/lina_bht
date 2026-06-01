from fastapi import (
    APIRouter,
    HTTPException
)


router = APIRouter(
    prefix="/legal",
    tags=["Profile"]
)


# ======================================
# INTERNAL FETCH
# ======================================

from app.core.mongo_client import legal_contents_collection

def fetch_page(page_id: str):
    doc = legal_contents_collection.find_one({"id": page_id})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Page not found"
        )
    doc["_id"] = str(doc["_id"])
    return doc



# ======================================
# GET PRIVACY
# ======================================

@router.get("/privacy")
async def get_privacy():

    return fetch_page("privacy")


# ======================================
# GET TERMS
# ======================================

@router.get("/terms")
async def get_terms():

    return fetch_page("terms")