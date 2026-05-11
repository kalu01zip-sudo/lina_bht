from fastapi import (
    APIRouter,
    HTTPException
)
from bson import ObjectId
import traceback
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    Field
)

from app.routers.auth import (
    CurrentUser
)

from app.services.product_routine_pipeline import (
    run_product_routine_pipeline
)

from app.services.manual_routine_pipeline import (
    generate_manual_ai_routine_draft,
    save_simple_manual_routine
)


router = APIRouter(

    prefix="/generate",

    tags=["Routine"]
)


# ==========================================
# REQUEST MODEL
# ==========================================

class ProductRoutineRequest(
    BaseModel
):

    product_scan_id: str


class ManualRoutineRequest(
    BaseModel
):

    product_name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "product_name",
            "product name",
            "productName"
        )
    )

    instruction: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "instruction",
            "instructions",
            "Instruction"
        )
    )

    time: Literal[
        "morning",
        "night",
        "weekly"
    ]


# ==========================================
# GENERATE PRODUCT ROUTINE
# ==========================================

@router.post("/product-routine")
async def generate_product_routine(

    body: ProductRoutineRequest,

    current_user: CurrentUser
):

    try:

        if not ObjectId.is_valid(
            body.product_scan_id
        ):

            raise HTTPException(

                status_code=400,

                detail="Invalid product_scan_id"
            )

        result = (
            await run_product_routine_pipeline(

                user_id=str(
                    current_user["_id"]
                ),

                product_scan_id=
                    body.product_scan_id
            )
        )

        return result

    except HTTPException:

        raise

    except Exception as e:

        print(
            "❌ PRODUCT ROUTINE ERROR:",
            e
        )

        traceback.print_exc()

        if str(e) == "Product scan not found":

            raise HTTPException(

                status_code=404,

                detail="Product scan not found"
            )

        if str(e) == "User not found":

            raise HTTPException(

                status_code=404,

                detail="User not found"
            )

        raise HTTPException(

            status_code=500,

            detail=
                "Failed to generate product routine"
        )


# ==========================================
# SIMPLE MANUAL ROUTINE
# ==========================================

@router.post("/manual-routine")
async def create_manual_routine(

    body: ManualRoutineRequest,

    current_user: CurrentUser
):

    try:

        return save_simple_manual_routine(

            user_id=str(
                current_user["_id"]
            ),

            product_name=body.product_name,

            instruction=body.instruction,

            time=body.time
        )

    except ValueError as e:

        raise HTTPException(

            status_code=400,

            detail=str(e)
        )

    except Exception as e:

        print(
            "❌ MANUAL ROUTINE ERROR:",
            e
        )

        traceback.print_exc()

        raise HTTPException(

            status_code=500,

            detail="Failed to save manual routine"
        )


# ==========================================
# AI MANUAL ROUTINE
# ==========================================

@router.post("/manual-routine/ai")
async def generate_manual_ai_routine(

    body: ManualRoutineRequest,

    current_user: CurrentUser
):

    try:

        return await generate_manual_ai_routine_draft(

            user_id=str(
                current_user["_id"]
            ),

            product_name=body.product_name,

            instruction=body.instruction,

            time=body.time,

            user_profile=current_user
        )

    except ValueError as e:

        raise HTTPException(

            status_code=400,

            detail=str(e)
        )

    except Exception as e:

        print(
            "❌ MANUAL AI ROUTINE ERROR:",
            e
        )

        traceback.print_exc()

        raise HTTPException(

            status_code=500,

            detail="Failed to generate manual routine"
        )
