# # routers/admin_products.py
# """
# ╔══════════════════════════════════════════════════════════════════╗
# ║         SkinSense — Admin Product Database                      ║
# ║                                                                  ║
# ║  All endpoints require a valid admin JWT                        ║
# ║  (Authorization: Bearer <admin_access_token>)                   ║
# ║                                                                  ║
# ║  Product records are stored in the 'products' MongoDB           ║
# ║  collection. They are auto-populated from user barcode scans    ║
# ║  (via /admin/products/sync-from-scans) and can be managed       ║
# ║  manually by admins.                                            ║
# ║                                                                  ║
# ║  Endpoints:                                                      ║
# ║   GET    /admin/products                  → paginated list      ║
# ║   POST   /admin/products                  → create product      ║
# ║   GET    /admin/products/routine-usage    → all products in any ║
# ║                                             user routine        ║
# ║   POST   /admin/products/sync-from-scans  → auto-populate DB   ║
# ║                                             from scan_results   ║
# ║   GET    /admin/products/{id}             → single product      ║
# ║   PUT    /admin/products/{id}             → edit product        ║
# ║   DELETE /admin/products/{id}             → delete product      ║
# ║   PUT    /admin/products/{id}/image       → upload or set image ║
# ║                                                                  ║
# ║  Image storage:                                                  ║
# ║   Admin can set image via:                                       ║
# ║     A) image_url   (string URL — from OBF, CDN, etc.)          ║
# ║     B) file upload (multipart — stored as base64 data URI)      ║
# ║   image_source tracks how the image was set:                    ║
# ║     "admin_url" | "admin_upload" | "barcode_scan"               ║
# ╚══════════════════════════════════════════════════════════════════╝
# """

# from __future__ import annotations

# import base64
# import logging
# import os
# from datetime import datetime, timezone
# from typing import Annotated, List, Literal, Optional

# from bson import ObjectId
# from fastapi import (
#     APIRouter, Depends, File, HTTPException, Query, UploadFile
# )
# from pydantic import BaseModel, Field

# from app.core.database import get_db
# from app.routers.admin_auth import _get_current_admin

# logger = logging.getLogger(__name__)

# router       = APIRouter(prefix="/admin/products", tags=["Admin Products"])
# CurrentAdmin = Annotated[dict, Depends(_get_current_admin)]

# # ── Constants ─────────────────────────────────────────────────────────────────

# MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 MB
# ALLOWED_MIMES   = {"image/jpeg", "image/png", "image/webp"}

# StatusType       = Literal["active", "inactive"]
# ImageSourceType  = Literal["admin_url", "admin_upload", "barcode_scan"]


# # ── DB shortcut ───────────────────────────────────────────────────────────────

# def _products_col():
#     return get_db()["products"]

# def _steps_col():
#     return get_db()["routine_steps"]

# def _scans_col():
#     return get_db()["scan_results"]

# def _oid(product_id: str) -> ObjectId:
#     try:
#         return ObjectId(product_id)
#     except Exception:
#         raise HTTPException(status_code=400, detail="Invalid product ID format.")

# def _utc_now() -> datetime:
#     return datetime.now(timezone.utc)

# def _sniff_mime(data: bytes) -> str:
#     if data[:3]  == b"\xff\xd8\xff":       return "image/jpeg"
#     if data[:8]  == b"\x89PNG\r\n\x1a\n":  return "image/png"
#     if data[8:12] == b"WEBP":              return "image/webp"
#     return "image/jpeg"


# # ── Schemas ───────────────────────────────────────────────────────────────────

# class ProductCreateRequest(BaseModel):
#     name:       str            = Field(..., min_length=1, max_length=200)
#     brand:      Optional[str]  = Field(None, max_length=120)
#     barcode:    Optional[str]  = Field(None, max_length=50)
#     category:   Optional[str]  = Field(None, max_length=80)
#     image_url:  Optional[str]  = Field(None, max_length=2000,
#                                        description="Direct image URL (OBF, CDN, etc.)")
#     status:     StatusType     = "active"

#     model_config = {"json_schema_extra": {"example": {
#         "name":     "Neutrogena Hydro Boost Water Gel",
#         "brand":    "Neutrogena",
#         "barcode":  "3600523457441",
#         "category": "Moisturiser",
#         "status":   "active",
#     }}}


# class ProductUpdateRequest(BaseModel):
#     """All fields optional — partial update."""
#     name:       Optional[str]        = Field(None, min_length=1, max_length=200)
#     brand:      Optional[str]        = Field(None, max_length=120)
#     barcode:    Optional[str]        = Field(None, max_length=50)
#     category:   Optional[str]        = Field(None, max_length=80)
#     image_url:  Optional[str]        = Field(None, max_length=2000)
#     status:     Optional[StatusType] = None

#     model_config = {"json_schema_extra": {"example": {
#         "brand":  "Neutrogena",
#         "status": "inactive",
#     }}}


# class ProductResponse(BaseModel):
#     id:            str
#     name:          str
#     brand:         Optional[str]
#     barcode:       Optional[str]
#     category:      Optional[str]
#     image_url:     Optional[str]
#     image_source:  Optional[str]     # "admin_url" | "admin_upload" | "barcode_scan"
#     has_image:     bool
#     status:        str
#     created_at:    str
#     updated_at:    str


# class RoutineUsageProduct(BaseModel):
#     """A product name found in any user's routine, with usage stats."""
#     product_name:    str
#     usage_count:     int             # total routine step entries
#     user_count:      int             # distinct users
#     time_slots:      List[str]       # which slots it appears in
#     in_product_db:   bool            # whether a matching record exists in products collection
#     product_db_id:   Optional[str]   # if in_product_db, the product _id


# class SyncResult(BaseModel):
#     synced:   int   # new product records created
#     skipped:  int   # already existed (matched by barcode or name+brand)
#     total:    int   # total scan_results with scan_type=product checked


# # ── Helpers ───────────────────────────────────────────────────────────────────

# def _fmt(doc: dict) -> ProductResponse:
#     created = doc.get("created_at")
#     updated = doc.get("updated_at")
#     image   = doc.get("image_url") or None
#     return ProductResponse(
#         id           = str(doc["_id"]),
#         name         = doc.get("name", ""),
#         brand        = doc.get("brand") or None,
#         barcode      = doc.get("barcode") or None,
#         category     = doc.get("category") or None,
#         image_url    = image,
#         image_source = doc.get("image_source") or None,
#         has_image    = bool(image),
#         status       = doc.get("status", "active"),
#         created_at   = created.isoformat() if isinstance(created, datetime) else str(created or ""),
#         updated_at   = updated.isoformat() if isinstance(updated, datetime) else str(updated or ""),
#     )


# # ══════════════════════════════════════════════════════════════════════════════
# #  Routes
# # ══════════════════════════════════════════════════════════════════════════════

# @router.get(
#     "",
#     response_model = List[ProductResponse],
#     summary        = "List all products in the product database",
#     description    = (
#         "Returns paginated product records from the `products` collection.\n\n"
#         "**Filters:**\n"
#         "- `search`      → partial match on name or brand (case-insensitive)\n"
#         "- `no_image`    → `true` returns only products with no profile image\n"
#         "- `status`      → `active` | `inactive` | omit for all\n"
#         "- `brand`       → exact brand name filter\n\n"
#         "**Pagination:** `limit` (max 200) + `skip`\n\n"
#         "Total count is returned in the `X-Total-Count` response header."
#     ),
# )
# async def list_products(
#     current_admin: CurrentAdmin,
#     search:        Optional[str] = Query(None, description="Search name or brand"),
#     no_image:      bool          = Query(False, description="Only return products without an image"),
#     status:        Optional[str] = Query(None, description="active | inactive"),
#     brand:         Optional[str] = Query(None, description="Exact brand filter"),
#     limit:         int           = Query(50,  ge=1, le=200),
#     skip:          int           = Query(0,   ge=0),
# ):
#     filt: dict = {}

#     if search:
#         filt["$or"] = [
#             {"name":  {"$regex": search, "$options": "i"}},
#             {"brand": {"$regex": search, "$options": "i"}},
#         ]

#     if no_image:
#         filt["$and"] = filt.get("$and", []) + [
#             {"$or": [{"image_url": None}, {"image_url": ""}, {"image_url": {"$exists": False}}]}
#         ]

#     if status in ("active", "inactive"):
#         filt["status"] = status

#     if brand:
#         filt["brand"] = {"$regex": f"^{brand}$", "$options": "i"}

#     col   = _products_col()
#     docs  = await col.find(filt).sort("name", 1).skip(skip).limit(limit).to_list(limit)
#     return [_fmt(d) for d in docs]


# @router.post(
#     "",
#     response_model = ProductResponse,
#     status_code    = 201,
#     summary        = "Create a new product record",
#     description    = (
#         "Adds a new product to the admin product database.\n\n"
#         "If a product with the same `barcode` already exists, "
#         "a 409 Conflict error is returned.\n\n"
#         "To set a profile image at creation time, provide `image_url`. "
#         "To upload an image file after creation, use "
#         "`PUT /admin/products/{id}/image`."
#     ),
# )
# async def create_product(
#     payload:       ProductCreateRequest,
#     current_admin: CurrentAdmin,
# ):
#     col = _products_col()

#     # Barcode uniqueness check
#     if payload.barcode:
#         existing = await col.find_one({"barcode": payload.barcode.strip()})
#         if existing:
#             raise HTTPException(
#                 status_code=409,
#                 detail=f"A product with barcode '{payload.barcode}' already exists (id: {existing['_id']}).",
#             )

#     now = _utc_now()
#     doc = {
#         "name":         payload.name.strip(),
#         "brand":        payload.brand.strip() if payload.brand else None,
#         "barcode":      payload.barcode.strip() if payload.barcode else None,
#         "category":     payload.category.strip() if payload.category else None,
#         "image_url":    payload.image_url.strip() if payload.image_url else None,
#         "image_source": "admin_url" if payload.image_url else None,
#         "status":       payload.status,
#         "created_at":   now,
#         "updated_at":   now,
#     }

#     result    = await col.insert_one(doc)
#     doc["_id"] = result.inserted_id
#     logger.info("Admin %s created product '%s' (%s)", current_admin.get("email"), doc["name"], result.inserted_id)
#     return _fmt(doc)


# @router.get(
#     "/routine-usage",
#     response_model = List[RoutineUsageProduct],
#     summary        = "All products used in any user's routine",
#     description    = (
#         "Aggregates the `routine_steps` collection across **all users** "
#         "to show every distinct product name that has been added to a routine.\n\n"
#         "Each entry includes:\n"
#         "- `usage_count`  — total routine step entries with that name\n"
#         "- `user_count`   — number of distinct users using it\n"
#         "- `time_slots`   — which slots (morning / night / weekly) it appears in\n"
#         "- `in_product_db` — whether a record exists in the admin product database\n"
#         "- `product_db_id` — the product record `_id` (if it exists)\n\n"
#         "Use this view to discover which popular routine products haven't been "
#         "catalogued in the product database yet."
#     ),
# )
# async def list_routine_usage(
#     current_admin:  CurrentAdmin,
#     search:         Optional[str] = Query(None, description="Filter by product name"),
#     not_in_db:      bool          = Query(False, description="Only show products not yet in product DB"),
#     limit:          int           = Query(100, ge=1, le=500),
#     skip:           int           = Query(0,   ge=0),
# ):
#     # Aggregate routine_steps by product_name
#     pipeline = [
#         {"$group": {
#             "_id":        {"$toLower": "$product_name"},
#             "product_name": {"$first": "$product_name"},
#             "usage_count":  {"$sum": 1},
#             "user_ids":     {"$addToSet": "$user_id"},
#             "time_slots":   {"$addToSet": "$time_slot"},
#         }},
#         {"$addFields": {"user_count": {"$size": "$user_ids"}}},
#         {"$sort": {"usage_count": -1}},
#     ]

#     if search:
#         pipeline.insert(0, {
#             "$match": {"product_name": {"$regex": search, "$options": "i"}}
#         })

#     all_rows = await _steps_col().aggregate(pipeline).to_list(None)

#     # Cross-reference with products collection
#     # Build a lookup: lowercase name → product doc
#     # Also check by name similarity (case-insensitive exact match)
#     product_docs = await _products_col().find(
#         {}, {"_id": 1, "name": 1}
#     ).to_list(None)

#     # Map lowercase name → id
#     db_name_map: dict[str, str] = {
#         d["name"].strip().lower(): str(d["_id"])
#         for d in product_docs
#     }

#     results: list[RoutineUsageProduct] = []
#     for row in all_rows:
#         name_lower   = (row.get("product_name") or "").strip().lower()
#         db_id        = db_name_map.get(name_lower)
#         in_db        = db_id is not None

#         if not_in_db and in_db:
#             continue

#         results.append(RoutineUsageProduct(
#             product_name  = row.get("product_name") or row["_id"],
#             usage_count   = row.get("usage_count", 0),
#             user_count    = row.get("user_count", 0),
#             time_slots    = sorted(set(t for t in (row.get("time_slots") or []) if t)),
#             in_product_db = in_db,
#             product_db_id = db_id,
#         ))

#     total = len(results)
#     paged = results[skip: skip + limit]

#     return paged


# @router.post(
#     "/sync-from-scans",
#     response_model = SyncResult,
#     summary        = "Auto-populate product DB from user product scans",
#     description    = (
#         "Scans the `scan_results` collection for all `scan_type=product` records "
#         "and upserts missing products into the `products` collection.\n\n"
#         "**Match logic (in order):**\n"
#         "1. If both records have a barcode → match on `barcode`\n"
#         "2. Otherwise → match on lowercase `product_name`\n\n"
#         "**Image:** if the scan result has a `product_image_url` (from Open Beauty Facts) "
#         "and the existing product record has no image, the image is automatically set "
#         "with `image_source = 'barcode_scan'`.\n\n"
#         "Safe to call multiple times — already-matched products are skipped."
#     ),
# )
# async def sync_from_scans(current_admin: CurrentAdmin):
#     scan_docs = await _scans_col().find(
#         {"scan_type": "product"},
#         {"product_name": 1, "brand": 1, "barcode": 1, "product_image_url": 1, "category": 1},
#     ).to_list(None)

#     synced  = 0
#     skipped = 0
#     col     = _products_col()

#     for doc in scan_docs:
#         pname   = (doc.get("product_name") or "").strip()
#         brand   = (doc.get("brand") or "").strip() or None
#         barcode = (doc.get("barcode") or "").strip() or None
#         img_url = (doc.get("product_image_url") or "").strip() or None
#         cat     = (doc.get("category") or "").strip() or None

#         if not pname:
#             skipped += 1
#             continue

#         # 1 — Try to find existing by barcode
#         existing = None
#         if barcode:
#             existing = await col.find_one({"barcode": barcode})

#         # 2 — Fall back to name match
#         if not existing:
#             existing = await col.find_one(
#                 {"name": {"$regex": f"^{pname}$", "$options": "i"}}
#             )

#         now = _utc_now()

#         if existing:
#             # Update image if missing and we have one from OBF
#             if img_url and not existing.get("image_url"):
#                 await col.update_one(
#                     {"_id": existing["_id"]},
#                     {"$set": {
#                         "image_url":    img_url,
#                         "image_source": "barcode_scan",
#                         "updated_at":   now,
#                     }},
#                 )
#             skipped += 1
#         else:
#             # Insert new product record
#             await col.insert_one({
#                 "name":         pname,
#                 "brand":        brand,
#                 "barcode":      barcode,
#                 "category":     cat,
#                 "image_url":    img_url,
#                 "image_source": "barcode_scan" if img_url else None,
#                 "status":       "active",
#                 "created_at":   now,
#                 "updated_at":   now,
#             })
#             synced += 1

#     logger.info(
#         "Admin %s ran sync-from-scans: %d synced, %d skipped, %d total",
#         current_admin.get("email"), synced, skipped, len(scan_docs),
#     )
#     return SyncResult(synced=synced, skipped=skipped, total=len(scan_docs))


# @router.get(
#     "/{product_id}",
#     response_model = ProductResponse,
#     summary        = "Get a single product by ID",
# )
# async def get_product(product_id: str, current_admin: CurrentAdmin):
#     oid = _oid(product_id)
#     doc = await _products_col().find_one({"_id": oid})
#     if not doc:
#         raise HTTPException(status_code=404, detail="Product not found.")
#     return _fmt(doc)


# @router.put(
#     "/{product_id}",
#     response_model = ProductResponse,
#     summary        = "Edit a product record",
#     description    = (
#         "Partial update — only include fields you want to change.\n\n"
#         "To change the image, use `PUT /admin/products/{id}/image` instead."
#     ),
# )
# async def update_product(
#     product_id:    str,
#     payload:       ProductUpdateRequest,
#     current_admin: CurrentAdmin,
# ):
#     oid = _oid(product_id)
#     col = _products_col()

#     doc = await col.find_one({"_id": oid})
#     if not doc:
#         raise HTTPException(status_code=404, detail="Product not found.")

#     updates: dict = {"updated_at": _utc_now()}

#     if payload.name     is not None: updates["name"]     = payload.name.strip()
#     if payload.brand    is not None: updates["brand"]    = payload.brand.strip() or None
#     if payload.barcode  is not None: updates["barcode"]  = payload.barcode.strip() or None
#     if payload.category is not None: updates["category"] = payload.category.strip() or None
#     if payload.status   is not None: updates["status"]   = payload.status

#     if payload.image_url is not None:
#         updates["image_url"]    = payload.image_url.strip() or None
#         updates["image_source"] = "admin_url" if payload.image_url.strip() else None

#     # Barcode uniqueness check (if changing barcode)
#     if "barcode" in updates and updates["barcode"]:
#         clash = await col.find_one({
#             "barcode": updates["barcode"],
#             "_id":     {"$ne": oid},
#         })
#         if clash:
#             raise HTTPException(
#                 status_code=409,
#                 detail=f"Another product already has barcode '{updates['barcode']}' (id: {clash['_id']}).",
#             )

#     await col.update_one({"_id": oid}, {"$set": updates})
#     logger.info("Admin %s updated product %s", current_admin.get("email"), product_id)
#     return _fmt(await col.find_one({"_id": oid}))


# @router.delete(
#     "/{product_id}",
#     summary = "Delete a product record",
# )
# async def delete_product(product_id: str, current_admin: CurrentAdmin):
#     oid    = _oid(product_id)
#     result = await _products_col().delete_one({"_id": oid})
#     if result.deleted_count == 0:
#         raise HTTPException(status_code=404, detail="Product not found.")
#     logger.info("Admin %s deleted product %s", current_admin.get("email"), product_id)
#     return {"success": True, "deleted_id": product_id}


# @router.put(
#     "/{product_id}/image",
#     response_model = ProductResponse,
#     summary        = "Upload or set a product profile image",
#     description    = (
#         "Set or replace the product's profile image. Accepts **two input modes** — "
#         "use only one per request:\n\n"
#         "**Mode A — File upload:**\n"
#         "Multipart form with `file` field. JPEG / PNG / WebP, max 5 MB.\n"
#         "Image is stored as a base64 data URI in MongoDB.\n\n"
#         "**Mode B — URL:**\n"
#         "Multipart form with `image_url` field (text). Stores the URL directly.\n\n"
#         "After update, `image_source` is set to `admin_upload` or `admin_url` "
#         "respectively. To remove an image, set `image_url` to an empty string."
#     ),
# )
# async def set_product_image(
#     product_id:    str,
#     current_admin: CurrentAdmin,
#     file:          Optional[UploadFile] = File(None,  description="Image file (JPEG/PNG/WebP, max 5 MB)"),
#     image_url:     Optional[str]        = None,
# ):
#     """
#     Accepts multipart form data. Either `file` or `image_url` must be provided,
#     but not both at the same time.
#     """
#     oid = _oid(product_id)
#     col = _products_col()

#     doc = await col.find_one({"_id": oid})
#     if not doc:
#         raise HTTPException(status_code=404, detail="Product not found.")

#     # ── Must provide at least one ─────────────────────────────────────────────
#     if not file and image_url is None:
#         raise HTTPException(
#             status_code=400,
#             detail="Provide either a 'file' upload or an 'image_url' string.",
#         )
#     if file and image_url:
#         raise HTTPException(
#             status_code=400,
#             detail="Provide either 'file' OR 'image_url', not both.",
#         )

#     updates: dict = {"updated_at": _utc_now()}

#     if file:
#         # ── Validate and read file ────────────────────────────────────────────
#         raw = await file.read()

#         if len(raw) > MAX_IMAGE_BYTES:
#             raise HTTPException(
#                 status_code=413,
#                 detail=f"Image too large. Maximum size is {MAX_IMAGE_BYTES // (1024*1024)} MB.",
#             )

#         mime = _sniff_mime(raw)
#         if mime not in ALLOWED_MIMES:
#             raise HTTPException(
#                 status_code=415,
#                 detail=f"Unsupported image format '{mime}'. Use JPEG, PNG, or WebP.",
#             )

#         b64_data = base64.standard_b64encode(raw).decode("utf-8")
#         updates["image_url"]    = f"data:{mime};base64,{b64_data}"
#         updates["image_source"] = "admin_upload"

#     else:
#         # ── URL mode ──────────────────────────────────────────────────────────
#         stripped = (image_url or "").strip()
#         updates["image_url"]    = stripped if stripped else None
#         updates["image_source"] = "admin_url" if stripped else None

#     await col.update_one({"_id": oid}, {"$set": updates})
#     logger.info(
#         "Admin %s set image on product %s (source: %s)",
#         current_admin.get("email"), product_id, updates["image_source"],
#     )
#     return _fmt(await col.find_one({"_id": oid}))
