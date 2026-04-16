# routers/routine.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Routine Management                          ║
║                                                                  ║
║  Endpoints:                                                      ║
║   GET    /routine                       → all steps (grouped)   ║
║   POST   /routine/step                  → add a step            ║
║   PATCH  /routine/step/{id}             → edit a step           ║
║   DELETE /routine/step/{id}             → delete a step         ║
║   POST   /routine/step/{id}/complete    → toggle completion     ║
║   GET    /routine/progress              → today's progress      ║
║   POST   /routine/step/{id}/ai-check    → AI product check      ║
║                                           (premium/trial only)  ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db, subscriptions_col
from routers.auth import _get_current_user

# ── Unified LLM client — works with Anthropic and LM Studio ──────────────────
from claude_client import ClaudeClient, USE_LOCAL_LLM

logger = logging.getLogger(__name__)

router      = APIRouter(prefix="/routine", tags=["Routine"])
CurrentUser = Annotated[dict, Depends(_get_current_user)]
TimeSlot    = Literal["morning", "night", "weekly"]


# ── Subscription gate ─────────────────────────────────────────────────────────

async def _require_premium(user: dict) -> None:
    sub = await subscriptions_col().find_one(
        {
            "user_id": str(user["_id"]),
            "status":  {"$in": ["active", "trialing"]},
        },
        sort=[("created_at", -1)],
    )
    if not sub:
        raise HTTPException(
            status_code=403,
            detail=(
                "AI Checking is a premium feature. "
                "Upgrade or start a free trial to unlock it."
            ),
        )


# ── Schemas ───────────────────────────────────────────────────────────────────

class AddStepRequest(BaseModel):
    time_slot:    TimeSlot       = Field(..., description="morning | night | weekly")
    product_name: str            = Field(..., min_length=1, max_length=120)
    instructions: Optional[str] = Field(None, max_length=300)

    model_config = {"json_schema_extra": {"example": {
        "time_slot": "morning",
        "product_name": "Vitamin C Serum",
        "instructions": "Apply 3 drops to damp skin before moisturiser.",
    }}}


class EditStepRequest(BaseModel):
    product_name: Optional[str]      = Field(None, min_length=1, max_length=120)
    instructions: Optional[str]      = Field(None, max_length=300)
    time_slot:    Optional[TimeSlot] = None


class RoutineStep(BaseModel):
    id:           str
    time_slot:    TimeSlot
    product_name: str
    title:        Optional[str]   = None   # AI-generated 2-4 word title
    bio:          Optional[str]   = None   # AI-generated 10-15 word summary
    description:  Optional[str]  = None   # AI-generated full HTML content
    instructions: Optional[str]           = None   # manual/legacy instructions
    order:        int
    is_completed: bool
    created_at:   datetime


class RoutineResponse(BaseModel):
    morning: list[RoutineStep]
    night:   list[RoutineStep]
    weekly:  list[RoutineStep]


class AiCheckResponse(BaseModel):
    is_good: bool
    reason:  str


# ── DB helpers ────────────────────────────────────────────────────────────────

def _col():
    return get_db()["routine_steps"]

def _today() -> str:
    return date.today().isoformat()

def _completed_today(doc: dict) -> bool:
    return doc.get("completed_date") == _today()

def _fmt(doc: dict) -> RoutineStep:
    return RoutineStep(
        id           = str(doc["_id"]),
        time_slot    = doc["time_slot"],
        product_name = doc["product_name"],
        title        = doc.get("title") or None,
        bio          = doc.get("bio") or None,
        description  = doc.get("description") or None,
        instructions = doc.get("instructions"),
        order        = doc.get("order", 0),
        is_completed = _completed_today(doc),
        created_at   = doc["created_at"],
    )

def _oid(step_id: str) -> ObjectId:
    try:
        return ObjectId(step_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid step ID format.")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=RoutineResponse, summary="Get all routine steps")
async def get_routine(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    docs    = await _col().find({"user_id": user_id}).sort("order", 1).to_list(200)

    grouped: dict[str, list] = {"morning": [], "night": [], "weekly": []}
    for doc in docs:
        slot = doc.get("time_slot", "morning")
        if slot in grouped:
            grouped[slot].append(_fmt(doc))

    return RoutineResponse(**grouped)


@router.post("/step", response_model=RoutineStep, status_code=201,
             summary="Add a routine step")
async def add_step(payload: AddStepRequest, current_user: CurrentUser):
    user_id = str(current_user["_id"])
    last = await _col().find_one(
        {"user_id": user_id, "time_slot": payload.time_slot},
        sort=[("order", -1)],
    )
    next_order = (last["order"] + 1) if last else 0

    doc = {
        "user_id":        user_id,
        "time_slot":      payload.time_slot,
        "product_name":   payload.product_name.strip(),
        "instructions":   payload.instructions.strip() if payload.instructions else None,
        "order":          next_order,
        "completed_date": None,
        "created_at":     datetime.now(timezone.utc),
    }
    result  = await _col().insert_one(doc)
    doc["_id"] = result.inserted_id
    return _fmt(doc)


@router.patch("/step/{step_id}", response_model=RoutineStep,
              summary="Edit a routine step")
async def edit_step(step_id: str, payload: EditStepRequest, current_user: CurrentUser):
    user_id = str(current_user["_id"])
    oid     = _oid(step_id)

    step = await _col().find_one({"_id": oid, "user_id": user_id})
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")

    updates: dict = {}
    if payload.product_name is not None: updates["product_name"] = payload.product_name.strip()
    if payload.instructions is not None: updates["instructions"] = payload.instructions.strip() or None
    if payload.time_slot    is not None: updates["time_slot"]    = payload.time_slot

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    await _col().update_one({"_id": oid}, {"$set": updates})
    return _fmt(await _col().find_one({"_id": oid}))


@router.delete("/step/{step_id}", summary="Delete a routine step")
async def delete_step(step_id: str, current_user: CurrentUser):
    user_id = str(current_user["_id"])
    oid     = _oid(step_id)
    result  = await _col().delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Step not found.")
    return {"success": True, "deleted_id": step_id}


@router.post("/step/{step_id}/complete", response_model=RoutineStep,
             summary="Toggle today's completion for a step")
async def toggle_complete(step_id: str, current_user: CurrentUser):
    user_id = str(current_user["_id"])
    oid     = _oid(step_id)

    step = await _col().find_one({"_id": oid, "user_id": user_id})
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")

    today    = _today()
    new_date = None if step.get("completed_date") == today else today

    await _col().update_one({"_id": oid}, {"$set": {"completed_date": new_date}})
    step["completed_date"] = new_date
    return _fmt(step)


@router.get("/progress", summary="Get today's completion progress per time slot")
async def get_progress(current_user: CurrentUser):
    user_id = str(current_user["_id"])
    today   = _today()
    docs    = await _col().find({"user_id": user_id}).to_list(200)

    slots: dict[str, dict] = {
        s: {"total": 0, "completed": 0} for s in ("morning", "night", "weekly")
    }
    for doc in docs:
        slot = doc.get("time_slot", "morning")
        if slot not in slots:
            continue
        slots[slot]["total"] += 1
        if doc.get("completed_date") == today:
            slots[slot]["completed"] += 1

    progress = []
    for slot, c in slots.items():
        t    = c["total"]
        done = c["completed"]
        progress.append({
            "time_slot":  slot,
            "total":      t,
            "completed":  done,
            "percentage": round(done / t * 100) if t else 0,
        })

    return {"progress": progress}


# ── AI Check ──────────────────────────────────────────────────────────────────

_AI_SYSTEM = """\
You are a professional dermatologist AI in a skincare app.
Determine if a skincare or haircare product is suitable for the user's profile.

Return ONLY valid JSON — no markdown, no extra text:
{
  "is_good": <true|false>,
  "reason":  "<one sentence, 10-15 words, specific and friendly, ends with a period>"
}

Rules:
- is_good = true  → product is safe and beneficial for this profile.
- is_good = false → product likely contains ingredients that conflict with skin type,
  trigger allergies, or worsen active concerns.
- reason: 10-15 words, one sentence, capital first letter, ends with period.
- Do NOT mention brand names. Focus on product category / ingredient type.
- Return ONLY the JSON. Nothing else.
""".strip()


def _build_prompt(product: str, user: dict) -> str:
    return (
        f"Product: {product}\n\n"
        f"User skin profile:\n"
        f"  Skin type:     {user.get('skin_type') or 'not specified'}\n"
        f"  Skin concerns: {', '.join(user.get('skin_concerns') or []) or 'none'}\n"
        f"  Hair type:     {user.get('hair_type') or 'not specified'}\n"
        f"  Hair concerns: {', '.join(user.get('hair_concerns') or []) or 'none'}\n"
        f"  Allergies:     {', '.join(user.get('allergies') or []) or 'none'}\n"
        f"  Current phase: {(user.get('current_phase') or 'none').replace('_', ' ')}\n\n"
        "Is this product good for this user? Return only the JSON."
    )


def _parse_ai_response(raw: str) -> AiCheckResponse:
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    def _load(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group())
            raise

    try:
        data = _load(clean)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"AI returned unparseable response: {raw[:200]}",
        )

    if "is_good" not in data or "reason" not in data:
        raise HTTPException(
            status_code=422,
            detail=f"AI response missing fields. Got: {data}",
        )

    return AiCheckResponse(is_good=bool(data["is_good"]), reason=str(data["reason"]).strip())


@router.post(
    "/step/{step_id}/ai-check",
    response_model = AiCheckResponse,
    summary        = "AI check — is this product right for my skin? (premium/trial only)",
    description    = (
        "**Premium & Trial users only.** Free users receive HTTP 403.\n\n"
        "Sends the step's product name + user skin profile to the AI.\n\n"
        "| `is_good` | Frontend display |\n"
        "|-----------|------------------|\n"
        "| `true`    | ✅ green tick    |\n"
        "| `false`   | ❌ red X         |\n\n"
        f"**Active LLM backend:** {'🏠 LM Studio (local)' if USE_LOCAL_LLM else '☁️ Anthropic Claude'}"
    ),
)
async def ai_check_step(step_id: str, current_user: CurrentUser):
    # 1 — Subscription gate
    await _require_premium(current_user)

    # 2 — Load the step
    user_id = str(current_user["_id"])
    oid     = _oid(step_id)
    step    = await _col().find_one({"_id": oid, "user_id": user_id})
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")

    product = step["product_name"]

    # 3 — Mock mode
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        logger.info("MOCK_MODE — mock AI check for '%s'", product)
        return AiCheckResponse(
            is_good=True,
            reason="This gentle product suits your skin type and concerns perfectly.",
        )

    # 4 — Call AI via unified client (routes to LM Studio or Anthropic)
    client = ClaudeClient()
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.text(system=_AI_SYSTEM, user=_build_prompt(product, current_user), max_tokens=128),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("AI check error: %s", exc)
        raise HTTPException(status_code=502, detail=f"AI error: {exc}")

    if not raw:
        raise HTTPException(status_code=502, detail="AI returned an empty response.")

    # 5 — Parse and return
    return _parse_ai_response(raw)