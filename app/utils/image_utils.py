import io
import logging
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

def optimise_image(
    image_bytes: bytes,
    media_type: str,
    max_px: int = 1024,
    quality: int = 82,
    max_bytes: int = 3670016,  # 3.5 MB (base64 will be ~4.7 MB, safely under 5MB)
) -> tuple[bytes, str]:
    """
    Optimises an image for Vision APIs (Claude/GPT):
    1. EXIF Transpose (fix rotation)
    2. Convert to RGB (required for JPEG)
    3. Resize if dimensions exceed max_px
    4. Iteratively reduce quality if byte size > max_bytes
    5. Iteratively resize if byte size still > max_bytes
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.error("Failed to open image for optimisation: %s", exc)
        return image_bytes, media_type

    # 1. Transpose based on EXIF
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # 2. Convert to RGB (required for JPEG)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 3. Resize if needed
    orig_w, orig_h = img.size
    max_dim = max(orig_w, orig_h)
    
    if max_dim > max_px:
        scale = max_px / max_dim
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        logger.debug("Resized image from %dx%d to %dx%d", orig_w, orig_h, new_w, new_h)

    # 4. Iteratively compress by quality
    current_quality = quality
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=current_quality, optimize=True)
    
    while buf.tell() > max_bytes and current_quality > 40:
        current_quality -= 10
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=current_quality, optimize=True)
        logger.debug("Reduced quality to %d, new size: %.2f MB", current_quality, buf.tell()/1024/1024)

    # 5. Iteratively resize if still too large
    while buf.tell() > max_bytes and img.width > 320:
        w, h = img.size
        new_w = int(w * 0.8)
        new_h = int(h * 0.8)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=current_quality, optimize=True)
        logger.debug("Resized further to %dx%d, new size: %.2f MB", new_w, new_h, buf.tell()/1024/1024)

    final_bytes = buf.getvalue()
    logger.info(
        "Optimised image: %dx%d -> %dx%d, %.2f MB -> %.2f MB",
        orig_w, orig_h, img.width, img.height,
        len(image_bytes)/1024/1024, len(final_bytes)/1024/1024
    )
    
    return final_bytes, "image/jpeg"
