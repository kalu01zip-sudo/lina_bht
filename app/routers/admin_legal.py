from fastapi import (
    APIRouter,
    HTTPException
)

from pydantic import BaseModel
from typing import List

from app.core.supabase_client import supabase


router = APIRouter(
    prefix="/admin/legal",
    tags=["Admin Legal"]
)


# ======================================
# MODELS
# ======================================

class LegalSection(BaseModel):

    order: int

    title: str

    content: str


class LegalRequest(BaseModel):

    page_title: str

    sections: List[LegalSection]

    footer_text: str


# ======================================
# INTERNAL UPSERT FUNCTION
# ======================================

def save_legal_page(
    page_id: str,
    data: LegalRequest
):

    existing = supabase.table(
        "legal_contents"
    ).select("id").eq(
        "id",
        page_id
    ).execute()

    payload = {

        "id": page_id,

        "page_title":
            data.page_title,

        "sections": [
            section.dict()
            for section in data.sections
        ],

        "footer_text":
            data.footer_text
    }

    # ==========================
    # UPDATE
    # ==========================

    if existing.data:

        response = supabase.table(
            "legal_contents"
        ).update(
            payload
        ).eq(
            "id",
            page_id
        ).execute()

        return response.data[0]

    # ==========================
    # CREATE
    # ==========================

    response = supabase.table(
        "legal_contents"
    ).insert(
        payload
    ).execute()

    return response.data[0]


# ======================================
# POST PRIVACY
# ======================================

@router.post("/privacy")
async def post_privacy(

    data: LegalRequest
):

    try:

        result = save_legal_page(
            "privacy",
            data
        )

        return {

            "success": True,

            "message":
                "Privacy updated",

            "data": result
        }

    except Exception as e:

        print("PRIVACY ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to save privacy"
        )


# ======================================
# POST TERMS
# ======================================

@router.post("/terms")
async def post_terms(

    data: LegalRequest
):

    try:

        result = save_legal_page(
            "terms",
            data
        )

        return {

            "success": True,

            "message":
                "Terms updated",

            "data": result
        }

    except Exception as e:

        print("TERMS ERROR:", e)

        raise HTTPException(
            status_code=500,
            detail="Failed to save terms"
        )