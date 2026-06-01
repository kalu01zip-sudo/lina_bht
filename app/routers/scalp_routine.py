from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.services.scalp_routine_ai import generate_scalp_routine_ai
from app.services.face_routine_builder import build_routine
from app.services.scalp_history import get_scalp_scan_by_id
from app.routers.auth import CurrentUser
from app.services.routine_draft_service import register_grouped_routine_drafts

router = APIRouter(prefix="/generate", tags=["Routine"])

class ScalpRoutineRequest(BaseModel):
    scan_id: str

@router.post("/scalp_hair")
async def generate_scalp_routine(
    body: ScalpRoutineRequest,
    current_user: CurrentUser
):
    try:
        # STEP 1: Fetch scan
        scan = get_scalp_scan_by_id(body.scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        # STEP 2: Security check
        if scan.get("user_id") != str(current_user["_id"]):
            raise HTTPException(status_code=403, detail="Unauthorized")

        # STEP 3: Data prep
        scan_data = {
            "analysis": scan.get("analysis"),
            "images": scan.get("images", [])
        }
        profile = {} # later: fetch from DB if needed

        # STEP 4: AI Routine
        ai_plan = await generate_scalp_routine_ai(scan_data, profile)

        # STEP 5: Build routine (reuse existing face routine builder if generic)
        # Assuming build_routine maps categories to products
        routine = build_routine(ai_plan)

        # STEP 6: Save drafts
        drafts = register_grouped_routine_drafts(
            user_id=str(current_user["_id"]),
            source="scalp",
            routine=routine,
            scan_id=body.scan_id
        )

        return {
            "scan_id": body.scan_id,
            "routine_step_id": [draft["routine_id"] for draft in drafts],
            "routine": routine
        }

    except HTTPException:
        raise
    except Exception as e:
        print("[ERROR] SCALP ROUTINE ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to generate scalp routine")
