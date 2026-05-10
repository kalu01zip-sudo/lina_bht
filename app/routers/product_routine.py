from fastapi import (
    APIRouter,
    HTTPException
)
from bson import ObjectId
import traceback

from pydantic import BaseModel

from app.routers.auth import (
    CurrentUser
)

from app.services.product_routine_pipeline import (
    run_product_routine_pipeline
)


router = APIRouter(

    prefix="/generate",

    tags=["Product Routine"]
)


# ==========================================
# REQUEST MODEL
# ==========================================

class ProductRoutineRequest(
    BaseModel
):

    product_scan_id: str


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
