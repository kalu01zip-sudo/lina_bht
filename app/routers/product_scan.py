from fastapi import (
    APIRouter,
    UploadFile,
    File,
    HTTPException,
    Query
)
from pydantic import BaseModel, Field
from pydantic import ConfigDict

from app.routers.auth import CurrentUser

from app.services.product_scan_pipeline import (
    run_product_scan_pipeline,
    run_product_code_scan_pipeline
)

from app.services.product_scan_history import (
    save_product_scan,
    get_recent_product_scans,
    get_product_scan_by_id
)

from app.services.product_catalog_service import (
    create_product_if_missing
)
from bson import ObjectId
from app.services.image_storage import upload_scan_image



router = APIRouter(
    prefix="/scan",
    tags=["Product Scan"]
)


class ProductCodeScanRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "3600523457441"
            }
        }
    )

    code: str = Field(
        ...,
        min_length=4,
        max_length=80,
        description="Barcode, QR code value, UPC, or EAN number."
    )


def _normalize_scan_code(
    code: str
):
    return str(code or "").strip()


# ==========================================
# PRODUCT SCAN
# ==========================================

@router.post("/product")
async def scan_product(
    current_user: CurrentUser,
    image: UploadFile = File(...)

):

    try:

        # ==================================
        # READ IMAGE
        # ==================================

        image_bytes = await image.read()

        # ==================================
        # AI ANALYSIS
        # ==================================

        ai_result = await run_product_scan_pipeline(

            user_id=str(current_user["_id"]),

            image_bytes=image_bytes
        )

        # ==================================
        # SAVE PRODUCT TO CATALOG (Skipped for /scan/product)
        # ==================================

        # We deliberately skip saving the product to the catalog/database for this endpoint.
        # The scan result will still be stored in the scan history.

        catalog_product = None
        image_url = None

        product_payload = {
            **ai_result["product"],
            "id": None,
            "image_url": None,
        }

        # ==================================
        # SAVE TO MONGO
        # ==================================

        product_scan_id = save_product_scan(

            user_id=str(current_user["_id"]),

            scan_data={

                "product": {

                    **product_payload
                },

                "analysis":
                    ai_result["analysis"],

                "detected_ingredients":
                    ai_result.get(
                        "detected_ingredients",
                        []
                    )
            }
        )

        # ==================================
        # RESPONSE
        # ==================================

        return {

            "scan_id":
                product_scan_id,

            **ai_result,

            "product":
                product_payload,

            "catalog_product":
                catalog_product
        }

    except Exception as e:

        print(
            "[ERROR] PRODUCT SCAN ERROR:",
            e
        )

        raise HTTPException(

            status_code=500,

            detail="Product scan failed"
        )


# ==========================================
# PRODUCT CODE SCAN
# ==========================================

@router.post("/code")
async def scan_product_code(
    payload: ProductCodeScanRequest,
    current_user: CurrentUser
):

    try:

        code = _normalize_scan_code(
            payload.code
        )

        ai_result = await run_product_code_scan_pipeline(

            user_id=str(current_user["_id"]),

            code=code
        )

        if not ai_result:

            raise HTTPException(

                status_code=404,

                detail={
                    "code": "PRODUCT_NOT_FOUND",
                    "message": "No cosmetic product found for this barcode or QR code value."
                }
            )

        catalog_product = create_product_if_missing(

            extracted_product={

                "product_name":
                    ai_result["product"].get(
                        "name"
                    ),

                "brand":
                    ai_result["product"].get(
                        "brand"
                    ),

                "category":
                    ai_result["product"].get(
                        "category"
                    ),

                "ingredients":
                    ai_result.get(
                        "detected_ingredients",
                        []
                    )
            },

            image_url=ai_result.get(
                "image_url"
            )
        )

        product_payload = {

            **ai_result["product"],

            "id":
                catalog_product.get(
                    "id"
                ),

            "image_url":
                catalog_product.get(
                    "image_url"
                )
        }

        product_scan_id = save_product_scan(

            user_id=str(current_user["_id"]),

            scan_data={

                "product": {

                    **product_payload
                },

                "analysis":
                    ai_result["analysis"],

                "detected_ingredients":
                    ai_result.get(
                        "detected_ingredients",
                        []
                    ),

                "barcode":
                    ai_result.get(
                        "barcode"
                    ),

                "data_source":
                    ai_result.get(
                        "data_source"
                    )
            }
        )

        response_payload = {
            key: value
            for key, value in ai_result.items()
            if key not in ("barcode", "data_source", "image_url")
        }

        return {

            "scan_id":
                product_scan_id,

            **response_payload,

            "product":
                product_payload,

            "catalog_product":
                catalog_product
        }

    except HTTPException:
        raise

    except Exception as e:

        print(
            "[ERROR] PRODUCT CODE SCAN ERROR:",
            e
        )

        raise HTTPException(

            status_code=500,

            detail="Product code scan failed"
        )
    
# ==========================================
# PRODUCT SCAN HISTORY
# ==========================================

@router.get("/product/history")
async def get_product_history(
    current_user: CurrentUser,
    limit: int = Query(50, ge=1, le=200, description="Limit the number of returned product scans"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):

    try:

        from datetime import datetime, timedelta
        from app.services.product_scan_history import product_scan_col
        since = datetime.utcnow() - timedelta(days=30 * 2)
        total = product_scan_col.count_documents({
            "user_id": str(current_user["_id"]),
            "created_at": {"$gte": since}
        })

        scans = get_recent_product_scans(

            user_id=str(current_user["_id"]),

            months=2,

            limit=limit,

            offset=offset
        )

        result = []

        for item in scans:

            result.append({

                "scan_id":
                    item["_id"],

                "product":
                    item.get("product"),

                "analysis": {

                    "overall_score":
                        item.get(
                            "analysis",
                            {}
                        ).get(
                            "overall_score"
                        )
                },

                "created_at":
                    item.get(
                        "created_at"
                    )
            })

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "history": result
        }

    except Exception as e:

        print(
            "[ERROR] PRODUCT HISTORY ERROR:",
            e
        )

        raise HTTPException(

            status_code=500,

            detail="Failed to fetch history"
        )
    
# ==========================================
# SINGLE PRODUCT SCAN
# ==========================================

@router.get("/product/{scan_id}")
async def get_single_product_scan(

    scan_id: str,

    current_user: CurrentUser
):

    try:

        scan = get_product_scan_by_id(
            scan_id
        )

        if not scan:

            raise HTTPException(

                status_code=404,

                detail="Scan not found"
            )

        # ==============================
        # SECURITY CHECK
        # ==============================

        if scan["user_id"] != str(
            current_user["_id"]
        ):

            raise HTTPException(

                status_code=403,

                detail="Unauthorized"
            )

        return scan

    except HTTPException:
        raise

    except Exception as e:

        print(
            "[ERROR] PRODUCT DETAIL ERROR:",
            e
        )

        raise HTTPException(

            status_code=500,

            detail="Failed to fetch product scan"
        )
