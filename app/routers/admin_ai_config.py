# app/routers/admin_ai_config.py
from datetime import datetime
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.mongo_client import db
from app.routers.admin_auth import _get_current_admin

router = APIRouter(prefix="/admin/ai-config", tags=["Admin AI Config"])
CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]

class AIConfigSaveRequest(BaseModel):
    tone: str = Field(..., description="Active personality tone preset")
    system_prompt_override: str = Field(..., description="System prompt override instructions")
    face_scan_model: str = Field("Face Scan Model v2.4", description="Face scan model name")
    face_scan_accuracy: str = Field("94.2%", description="Face scan model accuracy")
    face_scan_status: str = Field("Active", description="Face scan model status")
    scalp_hair_model: str = Field("Scalp & Hair Model v1.1", description="Scalp scan model name")
    scalp_hair_accuracy: str = Field("89.5%", description="Scalp scan model accuracy")
    scalp_hair_status: str = Field("Active", description="Scalp scan model status")
    ingredient_parser_model: str = Field("Ingredient Parser v3.0", description="Ingredient parser model name")
    ingredient_parser_accuracy: str = Field("99.1%", description="Ingredient parser model accuracy")
    ingredient_parser_status: str = Field("Active", description="Ingredient parser model status")

class APIKeyUpdateRequest(BaseModel):
    api_key: str = Field(..., description="Anthropic Claude API Key")


def _get_or_create_config() -> dict:
    col = db["ai_config"]
    config = col.find_one({"_id": "current"})
    if not config:
        config = {
            "_id": "current",
            "tone": "Professional & Empathetic",
            "system_prompt_override": "You are Lia, a warm, motivational, and knowledgeable skincare coach. Always prioritize barrier health and gentle ingredients. If a user reports severe pain or cystic acne, advise them to consult a dermatologist.",
            "face_scan_model": "Face Scan Model v2.4",
            "face_scan_accuracy": "94.2%",
            "face_scan_status": "Active",
            "scalp_hair_model": "Scalp & Hair Model v1.1",
            "scalp_hair_accuracy": "89.5%",
            "scalp_hair_status": "Active",
            "ingredient_parser_model": "Ingredient Parser v3.0",
            "ingredient_parser_accuracy": "99.1%",
            "ingredient_parser_status": "Active",
            "api_key": "",
            "monthly_quota": 1000000
        }
        col.insert_one(config)
    return config


def _get_monthly_api_calls() -> int:
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)

        # Count face scans, scalp scans, and product scans in current month
        face_scans = db["scan_results"].count_documents({"created_at": {"$gte": start_of_month}})
        
        # scalp_scans collection check
        scalp_scans = 0
        if "scalp_scans" in db.list_collection_names():
            scalp_scans = db["scalp_scans"].count_documents({"created_at": {"$gte": start_of_month}})
            
        # product_scan_history collection check
        product_scans = 0
        if "product_scan_history" in db.list_collection_names():
            product_scans = db["product_scan_history"].count_documents({"created_at": {"$gte": start_of_month}})

        # assistant replies in chat_messages collection represent API calls
        chats = 0
        if "chat_messages" in db.list_collection_names():
            chats = db["chat_messages"].count_documents({
                "created_at": {"$gte": start_of_month},
                "role": "assistant"
            })

        return face_scans + scalp_scans + product_scans + chats
    except Exception:
        return 0


@router.get("")
async def get_ai_config(current_admin: CurrentAdmin):
    config = _get_or_create_config()
    calls = _get_monthly_api_calls()
    quota = config.get("monthly_quota", 1000000)
    percentage = round((calls / quota) * 100, 1) if quota > 0 else 0.0

    # Mask key
    raw_key = config.get("api_key", "")
    masked_key = ""
    if raw_key:
        masked_key = raw_key[:7] + "•" * 15 + raw_key[-4:] if len(raw_key) > 12 else "••••••••••••"

    return {
        "success": True,
        "config": {
            "tone": config.get("tone"),
            "system_prompt_override": config.get("system_prompt_override"),
            "face_scan": {
                "name": config.get("face_scan_model"),
                "accuracy": config.get("face_scan_accuracy"),
                "status": config.get("face_scan_status")
            },
            "scalp_hair": {
                "name": config.get("scalp_hair_model"),
                "accuracy": config.get("scalp_hair_accuracy"),
                "status": config.get("scalp_hair_status")
            },
            "ingredient_parser": {
                "name": config.get("ingredient_parser_model"),
                "accuracy": config.get("ingredient_parser_accuracy"),
                "status": config.get("ingredient_parser_status")
            }
        },
        "api_usage": {
            "requests_used": calls,
            "monthly_quota": quota,
            "percentage": percentage,
            "api_key_configured": bool(raw_key),
            "masked_api_key": masked_key
        }
    }


@router.post("/save")
async def save_ai_config(body: AIConfigSaveRequest, current_admin: CurrentAdmin):
    db["ai_config"].update_one(
        {"_id": "current"},
        {"$set": {
            "tone": body.tone,
            "system_prompt_override": body.system_prompt_override,
            "face_scan_model": body.face_scan_model,
            "face_scan_accuracy": body.face_scan_accuracy,
            "face_scan_status": body.face_scan_status,
            "scalp_hair_model": body.scalp_hair_model,
            "scalp_hair_accuracy": body.scalp_hair_accuracy,
            "scalp_hair_status": body.scalp_hair_status,
            "ingredient_parser_model": body.ingredient_parser_model,
            "ingredient_parser_accuracy": body.ingredient_parser_accuracy,
            "ingredient_parser_status": body.ingredient_parser_status,
        }}
    )
    return {"success": True, "message": "AI configurations updated successfully"}


@router.post("/update-key")
async def update_api_key(body: APIKeyUpdateRequest, current_admin: CurrentAdmin):
    key = body.api_key.strip()
    if not key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid API key format. Must start with 'sk-'")
    
    db["ai_config"].update_one(
        {"_id": "current"},
        {"$set": {"api_key": key}}
    )
    return {"success": True, "message": "API key updated successfully"}


@router.post("/check-updates")
async def check_model_updates(current_admin: CurrentAdmin):
    # Simulates checking model updates
    return {
        "success": True,
        "message": "All diagnostic models are up to date.",
        "last_checked": datetime.utcnow().isoformat()
    }
