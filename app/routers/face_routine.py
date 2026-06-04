"""
app/routers/face_routine.py
────────────────────────────
POST /generate/face — Generate Face Routine

Optimizations + fixes applied:
  1. Cache check before AI call (same user + scan → return cached result)
  2. Real user profile passed to AI (was empty `{}` before)
  3. AI call is now async (non-blocking event loop)
  4. Saved routine categories fetched and passed to AI → no duplicate recommendations
  5. Builder-level dedup → no same category twice in one slot even if AI ignores rules
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.auth import CurrentUser
from app.services.face_routine_ai import generate_face_routine_ai
from app.services.face_routine_builder import build_routine
from app.services.routine_draft_service import register_grouped_routine_drafts
from app.services.scan_history import get_scan_by_id
from app.utils.routine_cache import get_cached_routine, set_cached_routine
from app.utils.saved_routine_filter import get_existing_categories

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/generate", tags=["Routine"])


class RoutineRequest(BaseModel):
    scan_id: str


@router.post("/face")
async def generate_face_routine(
    body: RoutineRequest,
    current_user: CurrentUser,
):
    try:
        user_id = str(current_user["_id"])

        # ── 1. Cache check ──────────────────────────────────────────────────
        cached = get_cached_routine("face", user_id, body.scan_id)
        if cached:
            logger.info("Face routine cache hit — user=%s scan=%s", user_id, body.scan_id)
            return cached

        # ── 2. Fetch scan ───────────────────────────────────────────────────
        scan = get_scan_by_id(body.scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        if scan.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Unauthorized")

        # ── 3. Build clean scan payload for AI ──────────────────────────────
        scan_data = {
            "analysis":   scan.get("analysis"),
            "nutritions": scan.get("nutritions", []),
        }

        # ── 4. Real user profile ─────────────────────────────────────────────
        profile = {
            "skin_type":     current_user.get("skin_type"),
            "skin_concerns": current_user.get("skin_concerns", []),
            "allergies":     current_user.get("allergies", []),
            "budget":        current_user.get("budget"),
            "current_phase": current_user.get("current_phase"),
        }

        # ── 5. Existing saved routine categories ─────────────────────────────
        existing_categories = get_existing_categories(user_id)
        logger.info(
            "Existing saved categories for user=%s: %s", user_id, existing_categories
        )

        # ── 6. AI routine plan (async, with dedup context) ──────────────────
        ai_plan = await generate_face_routine_ai(
            scan_data,
            profile,
            existing_categories=existing_categories,
        )

        # ── 7. Build + dedup (builder also removes saved-routine conflicts) ──
        routine = build_routine(ai_plan, excluded_categories=existing_categories)

        # ── 8. Draft registration ────────────────────────────────────────────
        drafts = register_grouped_routine_drafts(
            user_id=user_id,
            source="face",
            routine=routine,
            scan_id=body.scan_id,
        )

        response_data = {
            "scan_id":          body.scan_id,
            "routine_step_id":  [draft["routine_id"] for draft in drafts],
            "routine":          routine,
        }

        # ── 9. Cache result ──────────────────────────────────────────────────
        set_cached_routine("face", user_id, body.scan_id, response_data)

        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Face routine error for scan=%s: %s", body.scan_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate routine")
