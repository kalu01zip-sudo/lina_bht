"""
app/routers/scalp_routine.py
──────────────────────────────
POST /generate/scalp_hair — Generate Scalp Routine

Fixes applied:
  1. Cache check before AI call
  2. Real user profile passed to AI
  3. AI call is async (non-blocking)
  4. Saved routine categories fetched → no duplicate recommendations
  5. Builder-level dedup → hard filter even if AI ignores rules
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.auth import CurrentUser
from app.services.face_routine_builder import build_routine
from app.services.routine_draft_service import register_grouped_routine_drafts
from app.services.scalp_history import get_scalp_scan_by_id
from app.services.scalp_routine_ai import generate_scalp_routine_ai
from app.utils.routine_cache import get_cached_routine, set_cached_routine
from app.utils.saved_routine_filter import get_existing_categories

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/generate", tags=["Routine"])


class ScalpRoutineRequest(BaseModel):
    scan_id: str


@router.post("/scalp_hair")
async def generate_scalp_routine(
    body: ScalpRoutineRequest,
    current_user: CurrentUser,
):
    try:
        user_id = str(current_user["_id"])

        # ── 1. Cache check ──────────────────────────────────────────────────
        cached = get_cached_routine("scalp", user_id, body.scan_id)
        if cached:
            logger.info("Scalp routine cache hit — user=%s scan=%s", user_id, body.scan_id)
            return cached

        # ── 2. Fetch scan ───────────────────────────────────────────────────
        scan = get_scalp_scan_by_id(body.scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        if scan.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Unauthorized")

        # ── 3. Build clean scan payload for AI ──────────────────────────────
        scan_data = {
            "analysis": scan.get("analysis"),
            "images":   scan.get("images", []),
        }

        # ── 4. Real user profile ─────────────────────────────────────────────
        profile = {
            "hair_type":     current_user.get("hair_type"),
            "hair_concerns": current_user.get("hair_concerns", []),
            "skin_type":     current_user.get("skin_type"),
            "allergies":     current_user.get("allergies", []),
            "budget":        current_user.get("budget"),
            "current_phase": current_user.get("current_phase"),
        }

        # ── 5. Existing saved routine categories ─────────────────────────────
        existing_categories = get_existing_categories(user_id)
        logger.info(
            "Existing saved categories for user=%s: %s", user_id, existing_categories
        )

        # ── 6. AI routine plan ───────────────────────────────────────────────
        ai_plan = await generate_scalp_routine_ai(
            scan_data,
            profile,
            existing_categories=existing_categories,
        )

        # ── 7. Build + dedup ─────────────────────────────────────────────────
        routine = build_routine(ai_plan, excluded_categories=existing_categories)

        # ── 8. Draft registration ────────────────────────────────────────────
        drafts = register_grouped_routine_drafts(
            user_id=user_id,
            source="scalp",
            routine=routine,
            scan_id=body.scan_id,
        )

        response_data = {
            "scan_id":         body.scan_id,
            "routine_step_id": [draft["routine_id"] for draft in drafts],
            "routine":         routine,
        }

        # ── 9. Cache result ──────────────────────────────────────────────────
        set_cached_routine("scalp", user_id, body.scan_id, response_data)

        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Scalp routine error for scan=%s: %s", body.scan_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate scalp routine")
