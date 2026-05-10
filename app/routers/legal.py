from fastapi import (
    APIRouter,
    HTTPException
)

from app.core.supabase_client import supabase


router = APIRouter(
    prefix="/legal",
    tags=["Profile"]
)


# ======================================
# INTERNAL FETCH
# ======================================

def fetch_page(page_id: str):

    response = supabase.table(
        "legal_contents"
    ).select("*").eq(
        "id",
        page_id
    ).execute()

    if not response.data:

        raise HTTPException(
            status_code=404,
            detail="Page not found"
        )

    return response.data[0]


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