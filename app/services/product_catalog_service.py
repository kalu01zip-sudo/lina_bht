import io
import re
import uuid

from PIL import Image

from app.core.supabase_client import (
    supabase
)


PRODUCT_IMAGE_BUCKET = "assets"
PRODUCT_IMAGE_WIDTH = 240


# ==========================================
# FIND PRODUCT BY NAME
# ==========================================

def find_product_by_name(
    name: str
):

    if not name:

        return None

    response = supabase.table(
        "products"
    ).select("*").ilike(
        "name",
        name
    ).limit(1).execute()

    if response.data:

        return response.data[0]

    return None


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


def get_public_url(
    bucket: str,

    path: str
):

    url = supabase.storage.from_(
        bucket
    ).get_public_url(path)

    if isinstance(url, dict):

        return url.get("publicUrl")

    return url


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

    supabase.storage.from_(
        PRODUCT_IMAGE_BUCKET
    ).upload(
        path,
        resized,
        file_options={
            "content-type": "image/jpeg",
            "x-upsert": "true"
        }
    )

    return get_public_url(
        PRODUCT_IMAGE_BUCKET,
        path
    )


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

            updated = supabase.table(
                "products"
            ).update({
                "image_url": image_url
            }).eq(
                "id",
                existing["id"]
            ).execute()

            if updated.data:

                return updated.data[0]

            existing["image_url"] = image_url

        return existing

    base_id = normalize_product_id(
        product_name
    )

    product_id = base_id

    existing_id = supabase.table(
        "products"
    ).select("id").eq(
        "id",
        product_id
    ).execute()

    if existing_id.data:

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

    response = supabase.table(
        "products"
    ).insert(
        payload
    ).execute()

    if response.data:

        return response.data[0]

    return payload
