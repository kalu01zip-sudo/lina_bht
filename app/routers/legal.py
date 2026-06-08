from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

from app.core.mongo_client import legal_contents_collection
from app.routers.admin_auth import CurrentAdmin

router = APIRouter(
    prefix="/legal",
    tags=["Legal"]
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
# INTERNAL HELPER FUNCTIONS
# ======================================

def fetch_page(page_id: str):
    doc = legal_contents_collection.find_one({"id": page_id})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail="Page not found"
        )
    doc["_id"] = str(doc["_id"])
    return doc

def save_legal_page(page_id: str, data: LegalRequest):
    try:
        payload = {
            "id": page_id,
            "page_title": data.page_title,
            "sections": [
                section.model_dump() if hasattr(section, "model_dump") else section.dict()
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
# PRIVACY ENDPOINTS
# ======================================

@router.get("/privacy")
async def get_privacy():
    return fetch_page("privacy")

@router.post("/privacy")
async def post_privacy(
    data: LegalRequest,
    current_admin: CurrentAdmin
):
    try:
        result = save_legal_page("privacy", data)
        return {
            "success": True,
            "message": "Privacy updated",
            "data": result
        }
    except HTTPException:
        raise
    except Exception as e:
        print("PRIVACY ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to save privacy")


# ======================================
# TERMS ENDPOINTS
# ======================================

@router.get("/terms")
async def get_terms():
    return fetch_page("terms")

@router.post("/terms")
async def post_terms(
    data: LegalRequest,
    current_admin: CurrentAdmin
):
    try:
        result = save_legal_page("terms", data)
        return {
            "success": True,
            "message": "Terms updated",
            "data": result
        }
    except HTTPException:
        raise
    except Exception as e:
        print("TERMS ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to save terms")