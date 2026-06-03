# app/routers/articles.py
"""
Learn Articles — User-side ("Article") & Admin-side ("Admin") API endpoints.

Endpoints:
  USER:
    GET    /articles           → List all articles (lightweight, excludes content) with search/category filters
    GET    /articles/{id}      → Retrieve full article details & increment view count

  ADMIN:
    POST   /admin/articles      → Upload cover/video files & create new article
    GET    /admin/articles      → List all articles with full details
    PUT    /admin/articles/{id} → Update article details or re-upload files
    DELETE /admin/articles/{id} → Delete article
"""

import os
from datetime import datetime
from typing import Optional, List
from bson import ObjectId

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from pydantic import BaseModel

from app.routers.auth import CurrentUser
from app.routers.admin_auth import CurrentAdmin
from app.core.database import get_db
from app.core.s3_client import upload_file_to_s3

router = APIRouter(prefix="/articles", tags=["Article"])
admin_router = APIRouter(prefix="/admin/articles", tags=["Admin Articles"])


def articles_col():
    """Returns the articles collection from MongoDB."""
    return get_db()["articles"]


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class ArticleListResponse(BaseModel):
    id: str
    title: str
    description: str
    category: str
    read_time: str
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    views: int
    created_at: str


class ArticleListPageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    articles: List[ArticleListResponse]


class ArticleDetailResponse(BaseModel):
    id: str
    title: str
    description: str
    category: str
    read_time: str
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    content: str
    views: int
    created_at: str


# ══════════════════════════════════════════════════════════════════════════════
#  S3 UPLOAD HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def upload_file_to_s3_helper(bucket: str, file: UploadFile, file_path: str) -> str:
    """Helper to upload media (covers/videos) to S3 and get public URL."""
    try:
        file_bytes = await file.read()
        full_s3_path = f"{bucket}/{file_path}"
        url = await upload_file_to_s3(
            file_bytes,
            full_s3_path,
            file.content_type or "image/png"
        )
        return url
    except Exception as e:
        print(f"[ERROR] S3 UPLOAD ERROR ({bucket}):", e)
        raise HTTPException(500, f"Upload to S3 failed: {str(e)}")


def _fmt_article(doc: dict) -> dict:
    """Helper to format MongoDB document for response."""
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title", ""),
        "description": doc.get("description", ""),
        "category": doc.get("category", ""),
        "read_time": doc.get("read_time", ""),
        "image_url": doc.get("image_url"),
        "video_url": doc.get("video_url"),
        "content": doc.get("content", ""),
        "views": doc.get("views", 0),
        "created_at": doc.get("created_at", datetime.utcnow()).isoformat() if isinstance(doc.get("created_at"), datetime) else str(doc.get("created_at"))
    }


# ══════════════════════════════════════════════════════════════════════════════
#  USER-SIDE ENDPOINTS (tags=["Article"])
# ══════════════════════════════════════════════════════════════════════════════

@router.get("", response_model=ArticleListPageResponse)
async def list_articles(
    current_user: CurrentUser,
    search: Optional[str] = Query(None, description="Search term in title, description, or content"),
    category: Optional[str] = Query(None, description="Filter by category (e.g. Skin Health)"),
    recommended: Optional[bool] = Query(None, description="If true, sort by popularity (views)"),
    limit: int = Query(50, ge=1, le=200, description="Limit the number of returned articles"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """
    Fetch the list of articles.
    Lightweight response excluding the potentially large 'content' field.
    """
    query = {}

    # Category filter
    if category and category.lower() != "all":
        query["category"] = {"$regex": f"^{category}$", "$options": "i"}

    # Search filter
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"content": {"$regex": search, "$options": "i"}}
        ]

    # Sorting
    sort_opts = [("created_at", -1)]
    if recommended:
        sort_opts = [("views", -1), ("created_at", -1)]

    total = await articles_col().count_documents(query)
    cursor = articles_col().find(query, projection={"content": False})
    cursor.sort(sort_opts).skip(offset).limit(limit)

    articles = []
    async for doc in cursor:
        articles.append(_fmt_article(doc))

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "articles": articles
    }


@router.get("/categories", response_model=List[str])
async def get_article_categories(
    current_user: CurrentUser
):
    """
    Get a distinct list of all article categories currently present in the database.
    Useful for the frontend to dynamically render filter tabs/menus.
    """
    categories = await articles_col().distinct("category")
    cleaned = sorted(list({c.strip() for c in categories if c and c.strip()}))
    return cleaned


@router.get("/{article_id}", response_model=ArticleDetailResponse)
async def get_article_details(
    article_id: str,
    current_user: CurrentUser
):
    """
    Retrieve full article details by ID and dynamically increment view count by 1.
    """
    try:
        oid = ObjectId(article_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid article ID format")

    doc = await articles_col().find_one_and_update(
        {"_id": oid},
        {"$inc": {"views": 1}},
        return_document=True
    )

    if not doc:
        raise HTTPException(status_code=404, detail="Article not found")

    return _fmt_article(doc)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN-SIDE ENDPOINTS (tags=["Admin"])
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.post("", status_code=201)
async def create_article(
    current_admin: CurrentAdmin,
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    read_time: str = Form(...),
    content: str = Form(...),
    image_file: UploadFile = File(None),
    video_file: UploadFile = File(None)
):
    """
    Create a new article.
    Accepts multipart/form-data for image and video uploads.
    """
    article_id = str(ObjectId())

    # Cover image upload
    final_image_url = None
    if image_file:
        _, ext = os.path.splitext(image_file.filename or "")
        if not ext:
            ext = ".png"
        file_path = f"articles/{article_id}_cover{ext}"
        final_image_url = await upload_file_to_s3_helper("assets", image_file, file_path)

    # Video upload
    final_video_url = None
    if video_file:
        _, ext = os.path.splitext(video_file.filename or "")
        if not ext:
            ext = ".mp4"
        file_path = f"articles/{article_id}_video{ext}"
        final_video_url = await upload_file_to_s3_helper("routine-videos", video_file, file_path)

    doc = {
        "_id": ObjectId(article_id),
        "title": title,
        "description": description,
        "category": category,
        "read_time": read_time,
        "content": content,
        "image_url": final_image_url,
        "video_url": final_video_url,
        "views": 0,
        "created_at": datetime.utcnow()
    }

    await articles_col().insert_one(doc)

    return {
        "success": True,
        "message": "Article created successfully",
        "article": _fmt_article(doc)
    }


@admin_router.get("", response_model=List[ArticleDetailResponse])
async def admin_list_articles(
    current_admin: CurrentAdmin
):
    """
    Retrieve all articles with full details for admin dashboard.
    """
    cursor = articles_col().find()
    cursor.sort([("created_at", -1)])

    articles = []
    async for doc in cursor:
        articles.append(_fmt_article(doc))

    return articles

@admin_router.get("/{article_id}", response_model=ArticleDetailResponse)
async def admin_get_article(
    article_id: str,
    current_admin: CurrentAdmin,
):
    """Retrieve a single article by ID for admin purposes."""
    try:
        oid = ObjectId(article_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid article ID format")
    doc = await articles_col().find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Article not found")
    return _fmt_article(doc)


@admin_router.put("/{article_id}")
async def update_article(
    article_id: str,
    current_admin: CurrentAdmin,
    title: Optional[str] = Form(None, examples=[""]),
    description: Optional[str] = Form(None, examples=[""]),
    category: Optional[str] = Form(None, examples=[""]),
    read_time: Optional[str] = Form(None, examples=[""]),
    content: Optional[str] = Form(None, examples=[""]),
    image_file: UploadFile = File(None),
    video_file: UploadFile = File(None)
):
    """
    Update article fields or re-upload files.
    """
    try:
        oid = ObjectId(article_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid article ID format")

    existing = await articles_col().find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Article not found")

    updates = {}
    if title is not None:
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if category is not None:
        updates["category"] = category
    if read_time is not None:
        updates["read_time"] = read_time
    if content is not None:
        updates["content"] = content

    # Handle image upload
    if image_file:
        _, ext = os.path.splitext(image_file.filename or "")
        if not ext:
            ext = ".png"
        file_path = f"articles/{article_id}_cover{ext}"
        updates["image_url"] = await upload_file_to_s3_helper("assets", image_file, file_path)

    # Handle video upload
    if video_file:
        _, ext = os.path.splitext(video_file.filename or "")
        if not ext:
            ext = ".mp4"
        file_path = f"articles/{article_id}_video{ext}"
        updates["video_url"] = await upload_file_to_s3_helper("routine-videos", video_file, file_path)

    if updates:
        await articles_col().update_one({"_id": oid}, {"$set": updates})
        updated_doc = await articles_col().find_one({"_id": oid})
    else:
        updated_doc = existing

    return {
        "success": True,
        "message": "Article updated successfully",
        "article": _fmt_article(updated_doc)
    }


@admin_router.delete("/{article_id}")
async def delete_article(
    article_id: str,
    current_admin: CurrentAdmin
):
    """
    Delete an article.
    """
    try:
        oid = ObjectId(article_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid article ID format")

    res = await articles_col().delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")

    return {
        "success": True,
        "message": "Article deleted successfully"
    }
