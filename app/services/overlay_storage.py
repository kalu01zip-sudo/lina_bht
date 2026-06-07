# app/services/overlay_storage.py
"""
S3 upload helper for scan overlay images.

Stores generated condition overlays under::

    scan_overlays/{user_id}/{scan_id}/{condition_slug}.jpg

Reuses :func:`app.core.s3_client.upload_file_to_s3` for the actual upload.
"""

from __future__ import annotations

import logging
import re

from app.core.s3_client import upload_file_to_s3

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a condition name to a filesystem-safe slug.

    Example: ``"Acne / Pimples"`` → ``"acne_pimples"``
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


async def upload_overlay_to_s3(
    image_bytes: bytes,
    user_id: str,
    scan_id: str,
    condition_name: str,
) -> str:
    """
    Upload an overlay JPEG to S3.

    Parameters
    ----------
    image_bytes : bytes
        JPEG-encoded overlay image.
    user_id : str
        The authenticated user's ID.
    scan_id : str
        The MongoDB scan document ``_id``.
    condition_name : str
        Human-readable condition name (e.g. ``"Acne / Pimples"``).

    Returns
    -------
    str
        Public S3 URL of the uploaded overlay.
    """
    slug = _slugify(condition_name)
    s3_key = f"scan_overlays/{user_id}/{scan_id}/{slug}.jpg"

    url = await upload_file_to_s3(image_bytes, s3_key, "image/jpeg")
    logger.info("Uploaded overlay '%s' → %s", condition_name, url)
    return url
