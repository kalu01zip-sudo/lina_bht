# main.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Full Backend API  (MongoDB)                 ║
║                                                                  ║
║  Auth:    POST /auth/signup         POST /auth/verify-email     ║
║           POST /auth/resend-otp     POST /auth/signin           ║
║           POST /auth/google         POST /auth/apple            ║
║           POST /auth/refresh        POST /auth/signout          ║
║           POST /auth/forgot-password                            ║
║           POST /auth/reset-password POST /auth/change-password  ║
║           GET  /auth/me             PUT  /auth/me               ║
║                                                                  ║
║  Score:   POST /score                                           ║
║                                                                  ║
║  Scan:    POST /scan/face                                       ║
║           POST /scan/hair_scalp                                 ║
║           POST /scan/product                                    ║
║                                                                  ║
║  Routine: GET    /routine                                       ║
║           POST   /routine/step                                  ║
║           PATCH  /routine/step/{id}                             ║
║           DELETE /routine/step/{id}                             ║
║           POST   /routine/step/{id}/complete                    ║
║           GET    /routine/progress                              ║
║           POST   /routine/step/{id}/ai-check  (premium only)   ║
║                                                                  ║
║  Chat:    POST /chat/message   GET /chat/history                ║
║           DELETE /chat/history                                  ║
║                                                                  ║
║  Subs:    POST /subscription/verify                             ║
║           GET  /subscription/status                             ║
║           POST /subscription/cancel                             ║
║           POST /subscription/webhook                            ║
╚══════════════════════════════════════════════════════════════════╝

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

.env required:
    MONGO_URL, DB_NAME, SECRET_KEY, REFRESH_SECRET_KEY
    SMTP_USER, SMTP_PASSWORD
    USE_LOCAL_LLM=false          # true  → LM Studio (no API key needed)
    ANTHROPIC_API_KEY=...        # needed when USE_LOCAL_LLM=false
    LM_STUDIO_BASE_URL=http://localhost:1234/v1
    LM_STUDIO_MODEL=<model name from LM Studio UI>
    LM_STUDIO_VISION=true        # set only for VL models (e.g. Qwen2.5 VL 7B)
    MOCK_MODE=false              # true → skip all AI calls (fastest for UI dev)
"""

import os
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import httpx

load_dotenv()

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import create_indexes
from routers.auth            import router as auth_router
from routers.subscription    import router as subscription_router
from routers.score           import router as score_router
from routers.scan_face       import router as scan_face_router
from routers.scan_hair_scalp import router as scan_hair_scalp_router
from routers.scan_product    import router as scan_product_router
from routers.chat            import router as chat_router
from routers.routine         import router as routine_router


def _llm_label() -> str:
    mock      = os.getenv("MOCK_MODE",      "false").lower() == "true"
    use_local = os.getenv("USE_LOCAL_LLM",  "false").lower() == "true"
    lm_vision = os.getenv("LM_STUDIO_VISION","false").lower() == "true"
    model     = os.getenv("LM_STUDIO_MODEL", "local-model")

    if mock:
        return "⚠️  MOCK (no LLM calls)"
    if use_local:
        vision_tag = " + vision ✅" if lm_vision else " (text only, scans mocked)"
        return f"🏠 LM Studio — {model}{vision_tag}"
    return "☁️  Anthropic Claude"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_indexes()
    print(f"🚀 SkinSense API ready.  LLM = {_llm_label()}")
    yield
    print("🛑 Server shutdown.")


app = FastAPI(
    title       = "SkinSense API",
    description = "Authentication + AI Skin Analysis for SkinSense.",
    version     = "1.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],   # lock down in production
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

app.include_router(auth_router)
app.include_router(subscription_router)
app.include_router(score_router)
app.include_router(scan_face_router)
app.include_router(scan_hair_scalp_router)
app.include_router(scan_product_router)
app.include_router(chat_router)
app.include_router(routine_router)


@app.get("/health", tags=["Status"])
async def health():
    """
    Returns server status and active LLM backend info.
    Quick way to confirm the server is running and which AI is configured.
    """
    mock      = os.getenv("MOCK_MODE",       "false").lower() == "true"
    use_local = os.getenv("USE_LOCAL_LLM",   "false").lower() == "true"
    lm_vision = os.getenv("LM_STUDIO_VISION","false").lower() == "true"

    return {
        "status":  "ok",
        "server":  "SkinSense API v1.0.0",
        "llm": {
            "backend":       "lm_studio" if use_local else "anthropic",
            "mock_mode":     mock,
            "model":         os.getenv("LM_STUDIO_MODEL") if use_local else "claude-sonnet-4-6",
            "vision":        lm_vision if use_local else True,
            "label":         _llm_label(),
        },
    }

@app.get("/lm-status", tags=["Status"])
async def lm_status():
    """
    Checks LM Studio health using its native /api/v0/models endpoint.

    Returns per-model info including:
    - whether the model is loaded (loaded_instances not empty)
    - whether it supports vision (capabilities.vision)

    Fields:
    - `reachable`       → LM Studio server responded
    - `configured_model`→ LM_STUDIO_MODEL value from .env
    - `text_ready`      → configured model is loaded and running
    - `vision_enabled`  → LM_STUDIO_VISION=true in .env
    - `vision_ready`    → vision enabled AND loaded model supports vision
    - `models`          → full list of all models with their status
    - `use_local_llm`   → app is routing calls to LM Studio right now
    """
    use_local   = os.getenv("USE_LOCAL_LLM",    "false").lower() == "true"
    lm_vision   = os.getenv("LM_STUDIO_VISION", "false").lower() == "true"
    lm_base_url = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
    lm_model    = os.getenv("LM_STUDIO_MODEL",    "local-model")

    # Strip /v1 suffix to reach the native LM Studio REST API
    base = lm_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    api_url = f"{base}/api/v0/models"

    reachable    = False
    error_detail = None
    all_models   = []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(api_url)
            if resp.status_code == 200:
                reachable  = True
                raw_models = resp.json().get("models", [])

                for m in raw_models:
                    model_key     = m.get("key", "")
                    display_name  = m.get("display_name", model_key)
                    is_loaded     = len(m.get("loaded_instances", [])) > 0
                    has_vision    = m.get("capabilities", {}).get("vision", False)
                    model_type    = m.get("type", "llm")

                    all_models.append({
                        "key":          model_key,
                        "display_name": display_name,
                        "type":         model_type,
                        "loaded":       is_loaded,
                        "vision":       has_vision,
                        "params":       m.get("params_string"),
                        "quantization": m.get("quantization", {}).get("name"),
                    })
            else:
                error_detail = f"LM Studio returned HTTP {resp.status_code}"
    except httpx.ConnectError:
        error_detail = "Connection refused — LM Studio is not running or wrong port."
    except httpx.TimeoutException:
        error_detail = "Timed out connecting to LM Studio (5 s)."
    except Exception as exc:
        error_detail = f"Unexpected error: {exc}"

    # ── Find the configured model in the list ─────────────────────────────────
    configured = next(
        (m for m in all_models
         if lm_model.lower() in m["key"].lower() or m["key"].lower() in lm_model.lower()),
        None,
    )

    text_ready   = bool(configured and configured["loaded"])
    vision_ready = lm_vision and text_ready and bool(configured and configured["vision"])

    # ── Warn if configured model has no vision but VISION=true ────────────────
    vision_warning = None
    if lm_vision and configured and not configured["vision"]:
        vision_warning = (
            f"LM_STUDIO_VISION=true but '{configured['display_name']}' "
            f"does not support vision. Set LM_STUDIO_VISION=false or load a VL model."
        )

    # ── Suggest loaded models if configured one is not running ────────────────
    loaded_models = [m for m in all_models if m["loaded"] and m["type"] == "llm"]
    suggestion    = None
    if not text_ready and loaded_models:
        suggestion = (
            f"Configured model '{lm_model}' is not loaded. "
            f"Currently loaded: {[m['key'] for m in loaded_models]}. "
            f"Update LM_STUDIO_MODEL in .env to match."
        )

    return {
        "use_local_llm":     use_local,
        "lm_studio_url":     lm_base_url,
        "reachable":         reachable,
        "error":             error_detail,
        "configured_model":  lm_model,
        "text_ready":        text_ready,
        "vision_enabled":    lm_vision,
        "vision_ready":      vision_ready,
        "vision_warning":    vision_warning,
        "suggestion":        suggestion,
        "models":            all_models,
        "hint": (
            None if reachable else
            "Start LM Studio → top menu → Local Server → Start Server"
        ),
    }