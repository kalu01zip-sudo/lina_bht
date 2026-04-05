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
║  AI:    POST /scan   POST /profile/score   POST /product/scan   ║
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
"""

import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()   # load .env file

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import create_indexes
from routers.auth import router as auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_indexes()
    print("🚀 SkinSense API ready.")
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


@app.get("/health", tags=["Status"])
async def health():
    return {"status": "ok", "server": "SkinSense API v1.0.0"}
