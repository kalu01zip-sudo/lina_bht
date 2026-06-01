from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from app.core.mongo_client import legal_contents_collection

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
    try:
        payload = {
            "id": page_id,
            "page_title": data.page_title,
            "sections": [
                section.dict()
                for section in data.sections
            ],
            "footer_text": data.footer_text
        }

        # Update or Insert
        res = legal_contents_collection.update_one(
            {"id": page_id},
            {"$set": payload},
            upsert=True
        )
        
        doc = legal_contents_collection.find_one({"id": page_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc
    except Exception as e:
        print("SAVE LEGAL PAGE ERROR:", e)
        raise HTTPException(500, f"Database operation failed: {str(e)}")


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
            "message": "Privacy updated",
            "data": result
        }
    except HTTPException:
        raise
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
            "message": "Terms updated",
            "data": result
        }
    except HTTPException:
        raise
    except Exception as e:
        print("TERMS ERROR:", e)
        raise HTTPException(
            status_code=500,
            detail="Failed to save terms"
        )