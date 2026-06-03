import io
import re
import uuid
from datetime import datetime, timezone

from bson import ObjectId
from PIL import Image

from app.core.mongo_client import products_collection
from app.core.s3_client import _upload_file_sync

PRODUCT_IMAGE_WIDTH = 240


# ==========================================
# FIND PRODUCT BY NAME
# ==========================================

def find_product_by_name(
    name: str
):

    if not name:

        return None

    # Case-insensitive regex search
    import re as _re
    regex = _re.compile(_re.escape(name.strip()), _re.IGNORECASE)
    doc = products_collection.find_one({"name": regex})
    return serialize_product_doc(doc)


def serialize_product_doc(
    doc: dict | None
):
    if not doc:
        return None

    result = {**doc}

    if result.get("_id"):
        result["id"] = str(result.pop("_id"))
    elif result.get("id"):
        result["id"] = str(result["id"])

    return result


def product_id_filter(
    product_id: str
):
    if ObjectId.is_valid(str(product_id)):
        return {"_id": ObjectId(str(product_id))}

    return {"id": product_id}


# ==========================================
# NORMALIZE PRODUCT FIELDS
# ==========================================

def normalize_product_id(
    value: str
):

    cleaned = re.sub(
        r"[^a-z0-9]+",
        "_",
        (value or "").strip().lower()
    ).strip("_")

    return cleaned or f"product_{uuid.uuid4().hex[:8]}"


def clean_list(
    values
):

    if not values:

        return []

    result = []

    for value in values:

        item = str(value).strip().lower()

        if item and item not in result:

            result.append(item)

    return result


def clean_slug_list(
    values
):
    if not values:
        return []

    if isinstance(values, str):
        values = [values]

    result = []

    for value in values:
        item = str(value).strip().lower().replace(" ", "_")

        if item and item not in result:
            result.append(item)

    return result


def infer_detected_conditions(
    extracted_product: dict
):
    explicit = (
        extracted_product.get("detected_conditions")
        or extracted_product.get("concerns")
    )

    if explicit:
        return clean_slug_list(explicit)

    text = " ".join([
        str(extracted_product.get("product_name") or ""),
        str(extracted_product.get("name") or ""),
        str(extracted_product.get("category") or ""),
        " ".join(str(item) for item in extracted_product.get("ingredients", []) or []),
    ]).lower()

    mappings = [
        (("oilclear", "oil clear", "clay", "deep cleansing", "face wash", "cleanser"), "oiliness"),
        (("vitamin c", "ascorbic", "brightening", "glow"), "dullness"),
        (("niacinamide", "dark spot", "hyperpigmentation"), "uneven_tone"),
        (("retinol", "retinal", "bakuchiol"), "aging"),
        (("salicylic", "benzoyl peroxide", "tea tree", "acne"), "acne"),
        (("hyaluronic", "ceramide", "moistur", "hydrating"), "dryness"),
        (("sunscreen", "spf", "uv"), "sun_protection"),
        (("soothing", "centella", "cica", "aloe"), "redness"),
    ]

    detected = []

    for needles, condition in mappings:
        if any(needle in text for needle in needles):
            detected.append(condition)
            break

    return clean_slug_list(detected)


def get_s3_url(product_id: str) -> str:
    """Construct the S3 URL for a product image."""
    import os
    bucket = os.getenv("AWS_S3_BUCKET", "")
    region = os.getenv("AWS_REGION", "us-east-1")
    return f"https://{bucket}.s3.{region}.amazonaws.com/products/{product_id}.jpg"


def resize_product_image(
    image_bytes: bytes
):

    image = Image.open(
        io.BytesIO(image_bytes)
    )

    if image.mode != "RGB":

        image = image.convert("RGB")

    width, height = image.size

    if width > PRODUCT_IMAGE_WIDTH:

        ratio = PRODUCT_IMAGE_WIDTH / float(width)

        image = image.resize(
            (
                PRODUCT_IMAGE_WIDTH,
                int(height * ratio)
            ),
            Image.LANCZOS
        )

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="JPEG",
        quality=85
    )

    return buffer.getvalue()


def upload_resized_product_image(
    image_bytes: bytes,
    product_id: str
):
    resized = resize_product_image(
        image_bytes
    )

    path = f"products/{product_id}.jpg"

    _upload_file_sync(resized, path, "image/jpeg")

    return get_s3_url(product_id), path


# ==========================================
# CREATE PRODUCT
# ==========================================

def create_product_if_missing(

    extracted_product: dict,

    image_url: str = None,

    image_bytes: bytes = None
):

    product_name = (
        extracted_product.get("product_name")
        or extracted_product.get("name")
    )

    product_name = str(product_name).strip() if product_name else None

    if not product_name:

        raise ValueError(
            "Product name is required"
        )

    existing = find_product_by_name(
        product_name
    )

    if existing:
        existing_id = existing.get("id")
        updates = {}
        unset_fields = {}

        if "categories" not in existing:
            updates["categories"] = clean_slug_list(
                existing.get("category")
            )

        if not existing.get("detected_conditions"):
            updates["detected_conditions"] = (
                clean_slug_list(existing.get("concerns"))
                or infer_detected_conditions({
                    **extracted_product,
                    "category": existing.get("category") or extracted_product.get("category"),
                    "ingredients": existing.get("tags") or extracted_product.get("ingredients", []),
                })
            )

        if "created_at" not in existing:
            updates["created_at"] = datetime.now(timezone.utc)

        updates["updated_at"] = datetime.now(timezone.utc)

        for field in ("category", "tags", "concerns", "priority"):
            if field in existing:
                unset_fields[field] = ""

        if existing.get("id") and not existing.get("_id"):
            unset_fields["id"] = ""

        if updates or unset_fields:
            update_doc = {}
            if updates:
                update_doc["$set"] = updates
            if unset_fields:
                update_doc["$unset"] = unset_fields

            products_collection.update_one(
                product_id_filter(existing_id),
                update_doc
            )

            existing.update(updates)
            for field in unset_fields:
                existing.pop(field, None)

        if image_url and not existing.get(
            "image_url"
        ):

            products_collection.update_one(
                product_id_filter(existing_id),
                {"$set": {"image_url": image_url}}
            )

            existing["image_url"] = image_url

        if image_bytes and not existing.get(
            "image_url"
        ):

            image_url, s3_key = upload_resized_product_image(
                image_bytes=image_bytes,
                product_id=existing_id
            )

            products_collection.update_one(
                product_id_filter(existing_id),
                {"$set": {"image_url": image_url, "s3_key": s3_key}}
            )

            existing["image_url"] = image_url
            existing["s3_key"] = s3_key

        return existing

    product_id = str(ObjectId())
    s3_key = None

    if image_bytes and not image_url:

        image_url, s3_key = upload_resized_product_image(
            image_bytes=image_bytes,
            product_id=product_id
        )

    now = datetime.now(timezone.utc)

    payload = {

        "_id": ObjectId(product_id),

        "name":
            product_name,

        "image_url":
            image_url,

        "s3_key":
            s3_key,

        "categories":
            clean_slug_list(
                extracted_product.get(
                    "category",
                    ""
                )
            ),

        "detected_conditions":
            infer_detected_conditions(
                extracted_product
            ),

        "created_at":
            now,

        "updated_at":
            now
    }

    products_collection.insert_one({**payload})

    return serialize_product_doc(payload)
