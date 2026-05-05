from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.services.face_routine_ai import generate_face_routine_ai
from app.services.face_routine_builder import build_routine
from app.services.scan_history import get_scan_by_id
from app.routers.auth import CurrentUser


router = APIRouter(prefix="/generate", tags=["Routine"])


# =========================
# REQUEST MODEL
# =========================
class RoutineRequest(BaseModel):
    scan_id: str


# =========================
# ENDPOINT
# =========================
@router.post("/face")
async def generate_face_routine(
    body: RoutineRequest,
    current_user: CurrentUser
):
    try:
        # 🔥 STEP 1: Fetch scan from DB
        scan = get_scan_by_id(body.scan_id)

        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        # 🔒 STEP 2: Security check
        if scan.get("user_id") != str(current_user["_id"]):
            raise HTTPException(status_code=403, detail="Unauthorized")

        # 🔥 STEP 3: Extract only needed data (clean input to AI)
        scan_data = {
            "analysis": scan.get("analysis"),
            "nutritions": scan.get("nutritions", [])
        }

        # 🔥 STEP 4: (optional) user profile
        profile = {}  # later: fetch from DB

        # 🔥 STEP 5: AI routine plan
        ai_plan = await generate_face_routine_ai(scan_data, profile)

        # 🔥 STEP 6: Product mapping
        routine = build_routine(ai_plan)

        return {
            "scan_id": body.scan_id,
            "routine": routine
        }

    except HTTPException:
        raise

    except Exception as e:
        print("❌ ROUTINE ERROR:", e)
        raise HTTPException(status_code=500, detail="Failed to generate routine")