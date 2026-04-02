"""
╔══════════════════════════════════════════════════════════════════╗
║           SkinSense — FastAPI Server                            ║
║                                                                  ║
║  Endpoints:                                                      ║
║   POST /scan            → analyze uploaded image (all 4 models) ║
║   POST /profile/score   → score user skin & hair profile        ║
║   GET  /health          → server + model status                 ║
║   GET  /models          → list loaded models                    ║
╚══════════════════════════════════════════════════════════════════╝

Install:
    pip install -r requirements.txt

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Optional env vars:
    HF_TOKEN=hf_xxx        # for gated HuggingFace models
"""

import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from skinsense_analyzer import SkinSenseAnalyzer
from profile_scorer import ProfileScorer, UserProfile
from product_analyzer import ProductAnalyzer


# ─────────────────────────────────────────────────
#  SINGLETONS — created once, shared across requests
# ─────────────────────────────────────────────────

analyzer         = SkinSenseAnalyzer(hf_token=os.environ.get("HF_TOKEN"))
scorer           = ProfileScorer()
product_analyzer = ProductAnalyzer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load all ML models once at startup."""
    analyzer.load_models()
    yield
    print("🛑 Server shutting down.")


# ─────────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────────

app = FastAPI(
    title="SkinSense AI API",
    description="Multi-model skin & scalp analyzer for SkinSense mobile app.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restrict in production to your domain
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
MAX_FILE_SIZE  = 10 * 1024 * 1024   # 10 MB


# ─────────────────────────────────────────────────
#  REQUEST SCHEMA — /profile/score
# ─────────────────────────────────────────────────

class ProfileScoreRequest(BaseModel):
    current_phase:  Optional[str]       = None
    allergies:      Optional[list[str]] = []
    skin_type:      Optional[str]       = None
    skin_concerns:  Optional[list[str]] = []
    hair_type:      Optional[str]       = None
    hair_concerns:  Optional[list[str]] = []

    model_config = {
        "json_schema_extra": {
            "example": {
                "current_phase":  "pregnant",
                "allergies":      ["perfumes", "sulfates"],
                "skin_type":      "sensitive",
                "skin_concerns":  ["acne", "redness"],
                "hair_type":      "curly",
                "hair_concerns":  ["dandruff", "hair_fall"],
            }
        }
    }


# ─────────────────────────────────────────────────
#  ENDPOINTS
# ─────────────────────────────────────────────────

@app.get("/health", tags=["Status"])
async def health():
    """Returns server status and whether models are loaded."""
    return {
        "status":       "ok",
        "models_ready": analyzer._loaded,
        "server":       "SkinSense AI v1.0.0",
    }


@app.get("/models", tags=["Status"])
async def list_models():
    """Returns each model's load status.

    BUG FIX: Previously checked _pipe is not None for Model 2,
    which failed when Keras path was used (_pipe stays None but
    model IS loaded). Now uses the correct _loaded flag.
    """
    m = analyzer
    return {
        "models": [
            {
                "id":       1,
                "name":     "skintelligent-acne",
                "model_id": "imfarzanansari/skintelligent-acne",
                "task":     "Acne severity (Level -1 → 3)",
                # _pipe is set for pipeline models
                "loaded":   m.acne_model._pipe is not None,
            },
            {
                "id":       2,
                "name":     "skin-condition-classifier",
                "model_id": "Tanishq77/skin-condition-classifier",
                "task":     "6-class skin condition (Keras or pipeline fallback)",
                # FIX: use _loaded, not _pipe — _pipe is None when Keras is used
                "loaded":   m.condition_model._loaded,
                "mode":     m.condition_model._mode or "not loaded",
            },
            {
                "id":       3,
                "name":     "skin-type-classifier",
                "model_id": "dima806/skin_types_image_detection",
                "task":     "Skin type + disease classification",
                "loaded":   m.derma_model._primary is not None or m.derma_model._secondary is not None,
                "primary":  m.derma_model._primary is not None,
                "secondary": m.derma_model._secondary is not None,
            },
            {
                "id":       4,
                "name":     "YOLOv8-acne",
                "model_id": "Tinny-Robot/acne",
                "task":     "Pimple detection with bounding boxes",
                "loaded":   m.yolo_detector._model is not None,
            },
        ]
    }


@app.post("/scan", tags=["Analysis"])
async def scan_skin(
    file: UploadFile = File(..., description="Skin/face image. JPEG, PNG, WEBP. Max 10MB.")
):
    """
    Upload a skin or scalp image → full AI diagnosis from all 4 models.

    Returns: acne_severity, skin_conditions, dermatologic,
    pimple_detection (with bounding boxes), and a weighted summary.

    Example cURL:
        curl -X POST http://localhost:8000/scan -F "file=@face.jpg"

    React Native Axios:
        const form = new FormData();
        form.append('file', { uri: imageUri, type: 'image/jpeg', name: 'scan.jpg' });
        const res = await axios.post('http://YOUR_IP:8000/scan', form,
          { headers: { 'Content-Type': 'multipart/form-data' } });
    """
    # Validate content type
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type '{file.content_type}'. Accepted: {', '.join(ALLOWED_TYPES)}",
        )

    # Read and validate
    image_bytes = await file.read()
    if len(image_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Max 10MB.")
    if len(image_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded.")

    # Run analysis
    start = time.perf_counter()
    try:
        result = analyzer.analyze(image_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    return JSONResponse(content={
        "success":                 True,
        "filename":                file.filename,
        "processing_time_seconds": round(time.perf_counter() - start, 3),
        "diagnosis":               result,
    })


@app.post("/profile/score", tags=["Profile"])
async def score_profile(body: ProfileScoreRequest):
    """
    Submit user skin & hair profile → care intensity score (65–90).

    Score meaning:
    - 65–69 = Minimal care needed
    - 70–74 = Light care
    - 75–79 = Moderate care
    - 80–84 = Active care needed
    - 85–90 = Intensive care

    Valid inputs:
      current_phase : on_my_period | pregnant | postpartum
      allergies     : perfumes | essential_oils | sulfates | alcohol | any custom string
      skin_type     : dry | combination | normal | sensitive | oily
      skin_concerns : acne | pimple | irritation | redness | pigmentation | dullness | any custom
      hair_type     : straight | wavy | curly | coily | kinky
      hair_concerns : hair_fall | dandruff | oily_scalp | dry_scalp | any custom

    Example cURL:
        curl -X POST http://localhost:8000/profile/score
             -H "Content-Type: application/json"
             -d '{
               "skin_type": "sensitive",
               "skin_concerns": ["acne", "redness"],
               "current_phase": "pregnant",
               "hair_concerns": ["dandruff"]
             }'

    React Native Axios:
        const res = await axios.post('http://YOUR_IP:8000/profile/score', {
          skin_type: 'oily',
          skin_concerns: ['acne'],
          hair_concerns: ['dandruff'],
        });
        console.log(res.data.score);        // e.g. 76
        console.log(res.data.care_level);   // e.g. "Moderate care"
        console.log(res.data.top_priorities); // ["acne", "oily skin", ...]
    """
    profile = UserProfile(
        current_phase = body.current_phase,
        allergies     = body.allergies     or [],
        skin_type     = body.skin_type,
        skin_concerns = body.skin_concerns or [],
        hair_type     = body.hair_type,
        hair_concerns = body.hair_concerns or [],
    )

    try:
        result = scorer.score(profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scoring failed: {str(e)}")

    return JSONResponse(content={"success": True, **result})


@app.post("/product/scan", tags=["Product"])
async def scan_product(
    file:          UploadFile        = File(..., description="Image containing a barcode. JPEG, PNG, WEBP."),
    skin_type:     Optional[str]     = None,
    skin_concerns: Optional[str]     = None,   # comma-separated e.g. "acne,redness"
):
    """
    Scan a product barcode from a photo → ingredient analysis for your skin type.

    **How to use:**
    Take a clear photo of the product barcode and upload it here.
    Optionally pass your skin_type and skin_concerns to get personalised compatibility.

    **Steps inside:**
    1. Barcode decoded from image (pyzbar — EAN-13, UPC, QR supported)
    2. Product looked up on Open Beauty Facts (100,000+ cosmetics, free)
    3. Ingredients cross-referenced against 30+ known skin-type rules
    4. Compatibility score + warnings returned

    **skin_type values:** dry | oily | combination | sensitive | normal

    **skin_concerns values (comma-separated):** acne | pimple | redness | irritation | pigmentation | dullness

    **Example cURL:**
    ```
    curl -X POST http://localhost:8000/product/scan
         -F "file=@product_barcode.jpg"
         -F "skin_type=oily"
         -F "skin_concerns=acne,redness"
    ```

    **React Native Axios:**
    ```js
    const form = new FormData();
    form.append('file', { uri: imageUri, type: 'image/jpeg', name: 'barcode.jpg' });
    form.append('skin_type', 'oily');
    form.append('skin_concerns', 'acne,redness');
    const res = await axios.post('http://YOUR_IP:8000/product/scan', form,
      { headers: { 'Content-Type': 'multipart/form-data' } });
    ```
    """
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, detail=f"Unsupported type. Accepted: {', '.join(ALLOWED_TYPES)}")

    image_bytes = await file.read()
    if len(image_bytes) == 0:
        raise HTTPException(400, detail="Empty file.")
    if len(image_bytes) > MAX_FILE_SIZE:
        raise HTTPException(413, detail="File too large. Max 10MB.")

    # Parse skin_concerns from comma-separated string
    concerns_list = (
        [c.strip() for c in skin_concerns.split(",") if c.strip()]
        if skin_concerns else []
    )

    start = time.perf_counter()
    try:
        result = product_analyzer.scan(
            image_bytes   = image_bytes,
            skin_type     = skin_type,
            skin_concerns = concerns_list,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Product scan failed: {str(e)}")

    return JSONResponse(content={
        **result,
        "processing_time_seconds": round(time.perf_counter() - start, 3),
    })