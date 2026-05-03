from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import Annotated, List
from app.services.face_validation import validate_image
from app.services.face_ai import analyze_face_with_claude
import json

router = APIRouter(prefix="/scan", tags=["Face Scan"])


@router.post("/face")
async def upload_face_images(files: List[UploadFile] = File(...)):
    
    if len(files) != 5:
        raise HTTPException(400, "Exactly 5 images required")

    errors = []
    valid_count = 0
    valid_images = []
    results = []

    for i, file in enumerate(files):
        file_bytes = await file.read()  # ✅ read once

        if not file_bytes:
            errors.append({
                "image": i + 1,
                "error": "empty_file"
            })
            continue

        status = validate_image(file_bytes)

        if status == "ok":
            valid_count += 1
            valid_images.append(file_bytes)   # ✅ reuse later for Claude
            results.append(file.filename)
        else:
            errors.append({
                "image": i + 1,
                "error": status
            })

    # Minimum 3 valid images required
    if valid_count < 3:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Not enough valid images",
                "valid_images": valid_count,
                "errors": errors
            }
        )

    # Call Claude 
    try:
        ai_data = await analyze_face_with_claude(valid_images)
    except Exception as e:
        print("AI ERROR:", str(e))
        raise HTTPException(500, f"AI failed: {str(e)}")

    return ai_data