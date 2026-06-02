from fastapi import APIRouter, Depends
from app.routers.admin_auth import _get_current_admin

# Placeholder router for admin video endpoints (removed per user request)
router = APIRouter(prefix="/admin", tags=["Admin Upload"], dependencies=[Depends(_get_current_admin)])