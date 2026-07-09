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


def resize_to_1080x1350(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Resizes and crops the image to exactly 1080x1350px for YouCam HD API.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.error("Failed to open image for resizing: %s", exc)
        return image_bytes, "image/jpeg"

    # Transpose based on EXIF
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Convert to RGB (required for JPEG)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize and crop to exactly 1080x1350
    img = ImageOps.fit(img, (1080, 1350), method=Image.LANCZOS)
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


def resize_to_1080x1350(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Resizes and crops the image to exactly 1080x1350px for YouCam HD API.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.error("Failed to open image for resizing: %s", exc)
        return image_bytes, "image/jpeg"

    # Transpose based on EXIF
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Convert to RGB (required for JPEG)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Resize and crop to exactly 1080x1350
    img = ImageOps.fit(img, (1080, 1350), method=Image.LANCZOS)
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, optimize=True)
    
    return buf.getvalue(), "image/jpeg"

def crop_and_resize_face_to_1080x1350(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Detects the face in the original high-resolution image, crops a bounding box
    around it (including forehead and neck for 4:5 aspect ratio), and resizes 
    the result exactly to 1080x1350px for YouCam HD API.
    """
    try:
        import cv2
        import numpy as np
        from app.utils.face_detection_utils import detect_largest_face
        
        # 0. Fix EXIF orientation first before OpenCV loads it
        try:
            pil_init = Image.open(io.BytesIO(image_bytes))
            pil_init = ImageOps.exif_transpose(pil_init)
            if pil_init.mode != "RGB":
                pil_init = pil_init.convert("RGB")
            tmp_buf = io.BytesIO()
            pil_init.save(tmp_buf, format="JPEG", quality=100)
            oriented_bytes = tmp_buf.getvalue()
        except Exception as e:
            logger.warning("EXIF fix failed, using original bytes: %s", e)
            oriented_bytes = image_bytes
        
        # 1. Decode bytes to OpenCV format
        nparr = np.frombuffer(oriented_bytes, np.uint8)
        img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if img_cv is None:
            raise ValueError("Failed to decode image bytes to cv2 format.")
            
        img_h, img_w = img_cv.shape[:2]
        
        # 2. Detect face
        face_box = detect_largest_face(img_cv)
        
        # 3. Calculate target bounding box
        if face_box:
            x, y, w, h = face_box
            cx, cy = x + w / 2.0, y + h / 2.0
            
            logger.info("Face detected at x=%d, y=%d, w=%d, h=%d in %dx%d image", x, y, w, h, img_w, img_h)
            
            # Make the crop very tight to avoid 'error_src_face_too_small'
            # Target width = 1.4x face width (face takes ~71% of width)
            target_w = int(w * 1.4)
            target_h = int(target_w * (1350 / 1080)) # Maintain 4:5 aspect ratio
            
            # Ensure height is at least 1.4x face height
            if target_h < h * 1.4:
                target_h = int(h * 1.4)
                target_w = int(target_h * (1080 / 1350))
                
            # If target dimensions exceed image bounds, scale them down
            if target_w > img_w or target_h > img_h:
                scale = min(img_w / target_w, img_h / target_h)
                target_w = int(target_w * scale)
                target_h = int(target_h * scale)
                
            logger.info("Calculated target box: w=%d, h=%d", target_w, target_h)
                
            # Calculate top-left corner
            new_x = int(cx - target_w / 2.0)
            new_y = int(cy - target_h * 0.45) # Center face slightly above middle
            
            # Clamp to boundaries
            new_x = max(0, new_x)
            new_y = max(0, new_y)
            new_x2 = min(img_w, new_x + target_w)
            new_y2 = min(img_h, new_y + target_h)
            
            logger.info("Cropping image at x1=%d, y1=%d, x2=%d, y2=%d", new_x, new_y, new_x2, new_y2)
            
            # Crop
            cropped_cv = img_cv[new_y:new_y2, new_x:new_x2]
            
            # Convert to PIL for final resize
            cropped_rgb = cv2.cvtColor(cropped_cv, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(cropped_rgb)
        else:
            # Fallback if no face detected
            logger.warning("No face detected for cropping, falling back to original image")
            pil_img = Image.open(io.BytesIO(image_bytes))
            try:
                pil_img = ImageOps.exif_transpose(pil_img)
            except Exception:
                pass
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")

        # Resize exactly to 1080x1350
        pil_img = ImageOps.fit(pil_img, (1080, 1350), method=Image.LANCZOS)
        
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=95, optimize=True)
        
        return buf.getvalue(), "image/jpeg"
        
    except Exception as exc:
        logger.error("Face crop and resize failed: %s", exc)
        # Fallback to simple resize
        return resize_to_1080x1350(image_bytes)
