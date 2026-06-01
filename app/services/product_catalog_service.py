import io
import re
import uuid

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
    doc = products_collection.find_one({"name": regex}, {"_id": 0})
    return doc


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

    return get_s3_url(product_id)


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

        if image_bytes and not existing.get(
            "image_url"
        ):

            image_url = upload_resized_product_image(
                image_bytes=image_bytes,
                product_id=existing["id"]
            )

            products_collection.update_one(
                {"id": existing["id"]},
                {"$set": {"image_url": image_url}}
            )

            existing["image_url"] = image_url

        return existing

    base_id = normalize_product_id(
        product_name
    )

    product_id = base_id

    existing_id = products_collection.find_one({"id": product_id})

    if existing_id:

        product_id = f"{base_id}_{uuid.uuid4().hex[:8]}"

    if image_bytes and not image_url:

        image_url = upload_resized_product_image(
            image_bytes=image_bytes,
            product_id=product_id
        )

    payload = {

        "id": product_id,

        "name":
            product_name,

        "image_url":
            image_url,

        "category":
            str(
                extracted_product.get(
                    "category",
                    ""
                )
            ).strip().lower(),

        "tags":
            clean_list(
                extracted_product.get(
                    "ingredients",
                    []
                )
            ),

        "concerns": [],

        "priority": 99
    }

    products_collection.insert_one({**payload})
    # Remove _id from return value
    payload.pop("_id", None)

    return payload
