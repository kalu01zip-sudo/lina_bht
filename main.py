# main.py
"""
╔══════════════════════════════════════════════════════════════════╗
║         SkinSense — Full Backend API  (MongoDB)                 ║
║                                                                  ║
║  Auth:  POST /auth/signup       POST /auth/verify-email         ║
║         POST /auth/resend-otp   POST /auth/signin               ║
║         POST /auth/google       POST /auth/refresh              ║
║         POST /auth/signout      POST /auth/forgot-password      ║
║         POST /auth/reset-password  POST /auth/change-password   ║
║         GET  /auth/me           PUT  /auth/me                   ║
║                                                                  ║
║  Score: POST /score             ← Skin Health Score             ║
║                                                                  ║
║  Scan:  POST /scan/face         ← Face skin analysis (Vision)   ║
║         POST /scan/hair_scalp   ← Hair & scalp analysis (Vision) ║
║         POST /scan/barcode      ← coming soon                   ║
╚══════════════════════════════════════════════════════════════════╝

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

.env required:
    MONGO_URL=mongodb://localhost:27017
    DB_NAME=skinsense
    SECRET_KEY=...
    REFRESH_SECRET_KEY=...
    SMTP_USER=your@gmail.com
    SMTP_PASSWORD=your-app-password
    ANTHROPIC_API_KEY=...
    MOCK_MODE=false           # set true to skip API calls in development
"""

import os
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import create_indexes
from routers.auth         import router as auth_router
from routers.subscription import router as subscription_router
from routers.score        import router as score_router
from routers.scan_face       import router as scan_face_router
from routers.scan_hair_scalp import router as scan_hair_scalp_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_indexes()
    mock = os.getenv("MOCK_MODE", "false").lower() == "true"
    print(f"🚀 SkinSense API ready.  MOCK_MODE={'ON ⚠️' if mock else 'off'}")
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


@app.get("/health", tags=["Status"])
async def health():
    return {
        "status":    "ok",
        "server":    "SkinSense API v1.0.0",
        "mock_mode": os.getenv("MOCK_MODE", "false").lower() == "true",
    }