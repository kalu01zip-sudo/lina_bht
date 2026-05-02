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
from app.core.database import create_indexes
from app.routers.auth               import router as auth_router
from app.routers.subscription       import router as subscription_router
from app.routers.score              import router as score_router
from app.routers.scan_face          import router as scan_face_router
from app.routers.scan_hair_scalp    import router as scan_hair_scalp_router
from app.routers.scan_product       import router as scan_product_router
from app.routers.scan_details       import router as scan_details_router        # ← NEW
from app.routers.routine_generate   import router as routine_generate_router    # ← NEW
from app.routers.chat               import router as chat_router
from app.routers.routine            import router as routine_router
from app.routers.scan_barcode_check import router as scan_barcode_check_router
from app.routers.admin_auth         import router as admin_auth_router
from app.routers.admin_home         import router as admin_home_router
from app.routers.admin_products      import router as admin_products_router      # ← NEW
from app.routers.admin_subscription  import router as admin_subscription_router  # ← NEW
from app.routers.admin_analytics import router as admin_analytics_router
from app.routers import onboarding

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
    version     = "1.2.0",
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
app.include_router(scan_details_router)       # GET /scan/{type}/details/{id}
app.include_router(routine_generate_router)   # POST /scan/{type}/{id}/generate-routine
app.include_router(chat_router)
app.include_router(routine_router)
app.include_router(scan_barcode_check_router)
app.include_router(admin_auth_router)          # POST /admin/auth/...
app.include_router(admin_home_router)          # GET  /admin/home/...
app.include_router(admin_products_router)      # CRUD /admin/products/...  ← NEW
app.include_router(admin_subscription_router)  # GET/PATCH /admin/subscription/... ← NEW
app.include_router(admin_analytics_router)
app.include_router(onboarding.router)


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
        "status": "ok",
        "server": "SkinSense API v1.2.0",
        "llm": {
            "backend":   "lm_studio" if use_local else "anthropic",
            "mock_mode": mock,
            "model":     os.getenv("LM_STUDIO_MODEL") if use_local else "claude-sonnet-4-6",
            "vision":    lm_vision if use_local else True,
            "label":     _llm_label(),
        },
    }


@app.get("/lm-status", tags=["Status"])
async def lm_status():
    """
    Checks LM Studio health using its native /api/v0/models endpoint.
    """
    use_local   = os.getenv("USE_LOCAL_LLM",    "false").lower() == "true"
    lm_vision   = os.getenv("LM_STUDIO_VISION", "false").lower() == "true"
    lm_base_url = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
    lm_model    = os.getenv("LM_STUDIO_MODEL",    "local-model")

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
                    model_key    = m.get("key", "")
                    is_loaded    = len(m.get("loaded_instances", [])) > 0
                    has_vision   = m.get("capabilities", {}).get("vision", False)
                    all_models.append({
                        "key":          model_key,
                        "display_name": m.get("display_name", model_key),
                        "type":         m.get("type", "llm"),
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

    configured = next(
        (m for m in all_models
         if lm_model.lower() in m["key"].lower() or m["key"].lower() in lm_model.lower()),
        None,
    )
    text_ready   = bool(configured and configured["loaded"])
    vision_ready = lm_vision and text_ready and bool(configured and configured["vision"])

    vision_warning = None
    if lm_vision and configured and not configured["vision"]:
        vision_warning = (
            f"LM_STUDIO_VISION=true but '{configured['display_name']}' "
            f"does not support vision. Set LM_STUDIO_VISION=false or load a VL model."
        )

    loaded_models = [m for m in all_models if m["loaded"] and m["type"] == "llm"]
    suggestion    = None
    if not text_ready and loaded_models:
        suggestion = (
            f"Configured model '{lm_model}' is not loaded. "
            f"Currently loaded: {[m['key'] for m in loaded_models]}. "
            f"Update LM_STUDIO_MODEL in .env to match."
        )

    return {
        "use_local_llm":    use_local,
        "lm_studio_url":    lm_base_url,
        "reachable":        reachable,
        "error":            error_detail,
        "configured_model": lm_model,
        "text_ready":       text_ready,
        "vision_enabled":   lm_vision,
        "vision_ready":     vision_ready,
        "vision_warning":   vision_warning,
        "suggestion":       suggestion,
        "models":           all_models,
        "hint": (
            None if reachable else
            "Start LM Studio → top menu → Local Server → Start Server"
        ),
    }
