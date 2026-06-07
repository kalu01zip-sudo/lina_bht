# app/utils/overlay_utils.py
"""
Overlay image generation for face scan conditions.

Uses OpenCV to draw condition-specific annotations (rectangles, semi-transparent
fills, labels) on face images based on normalised bounding-box coordinates
returned by Claude Vision.

v2 — ZOOMED FACE OVERLAYS
  - Detects the face via Haar cascade and crops to face + padding
  - Remaps normalised bounding-box coordinates to the cropped region
  - Draws precise, professional annotations on the zoomed face

Two overlay styles:
  - Rectangle borders  → localized conditions (acne, blackheads, pores …)
  - Semi-transparent fill → diffuse conditions (redness, oiliness, dullness …)

All 16 trigger types from the scan prompt have a dedicated colour.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from app.utils.face_detection_utils import detect_face_with_keypoints

logger = logging.getLogger(__name__)

# ── Condition colour map (BGR for OpenCV) ─────────────────────────────────────

CONDITION_COLORS: dict[str, tuple[int, int, int]] = {
    "acne / pimples":                 (0, 255, 255),    # Yellow
    "blackheads":                     (0, 200, 220),    # Amber
    "whiteheads":                     (220, 220, 220),  # Light Gray
    "oiliness":                       (0, 230, 255),    # Light Yellow
    "dehydration":                    (255, 180, 0),    # Blue
    "dark spots / hyperpigmentation": (200, 0, 180),    # Purple
    "uneven skin tone":               (200, 0, 200),    # Magenta
    "redness / irritation":           (0, 0, 255),      # Red
    "enlarged pores":                 (255, 255, 0),    # Cyan
    "fine lines / wrinkles":          (0, 165, 255),    # Orange
    "dark circles":                   (130, 0, 75),     # Indigo
    "puffiness":                      (255, 200, 150),  # Light Blue
    "eczema / dry patches":           (180, 105, 255),  # Pink
    "rosacea":                        (100, 0, 230),    # Deep Pink
    "sun damage":                     (50, 100, 255),   # Coral
    "dullness":                       (160, 160, 160),  # Gray
}

# Conditions that use semi-transparent FILL instead of rectangle borders.
# These are typically area-wide / diffuse and look better with a tint.
_FILL_CONDITIONS: set[str] = {
    "oiliness", "dehydration", "uneven skin tone",
    "redness / irritation", "puffiness", "rosacea", "dullness",
}

_DEFAULT_COLOR: tuple[int, int, int] = (0, 255, 0)  # Green fallback
_JPEG_QUALITY   = 88

# ── Zoomed overlay style constants ───────────────────────────────────────────
_RECT_THICKNESS   = 2        # thinner, precise rectangles
_FILL_ALPHA       = 0.25     # subtler fills
_LABEL_FONT       = cv2.FONT_HERSHEY_SIMPLEX
_LABEL_SCALE      = 0.55     # smaller, cleaner text
_LABEL_THICK      = 1
_CORNER_LEN_RATIO = 0.15     # corner bracket length as fraction of box size
_FACE_PAD_RATIO   = 0.20     # 20% padding around detected face


# ── Face detection + cropping ────────────────────────────────────────────────


def _detect_and_crop_face(
    img: np.ndarray,
    pad_ratio: float = _FACE_PAD_RATIO,
) -> tuple[np.ndarray, tuple[int, int, int, int] | None, list[dict]]:
    """
    Detect the largest face and return (cropped_image, (cx1, cy1, cx2, cy2), keypoints).

    The crop region includes *pad_ratio* padding around the face.
    If no face is found, returns (original_image, None, []) as a fallback.
    """
    img_h, img_w = img.shape[:2]

    face_data = detect_face_with_keypoints(img)
    if face_data is None:
        logger.info("No face detected for crop — using full image as fallback")
        return img, None, []

    fx, fy, fw, fh = face_data["box"]
    keypoints = face_data["keypoints"]

    # Expand with padding
    pad_x = int(fw * pad_ratio)
    pad_y = int(fh * pad_ratio)
    cx1 = max(0, fx - pad_x)
    cy1 = max(0, fy - pad_y)
    cx2 = min(img_w, fx + fw + pad_x)
    cy2 = min(img_h, fy + fh + pad_y)

    cropped = img[cy1:cy2, cx1:cx2].copy()
    return cropped, (cx1, cy1, cx2, cy2), keypoints


def _remap_region_to_crop(
    region: dict,
    img_w: int,
    img_h: int,
    crop_box: tuple[int, int, int, int],
) -> dict | None:
    """
    Remap a normalised bounding box from full-image space into cropped-face space.

    Returns a new region dict with coordinates relative to the crop, or None
    if the region doesn't overlap the crop at all.
    """
    cx1, cy1, cx2, cy2 = crop_box
    crop_w = cx2 - cx1
    crop_h = cy2 - cy1

    if crop_w <= 0 or crop_h <= 0:
        return None

    # Convert normalised region to pixel coords in full image
    rx = float(region.get("x", 0))
    ry = float(region.get("y", 0))
    rw = float(region.get("width", 0))
    rh = float(region.get("height", 0))

    px1 = rx * img_w
    py1 = ry * img_h
    px2 = (rx + rw) * img_w
    py2 = (ry + rh) * img_h

    # Clip to crop area
    clipped_x1 = max(px1, cx1) - cx1
    clipped_y1 = max(py1, cy1) - cy1
    clipped_x2 = min(px2, cx2) - cx1
    clipped_y2 = min(py2, cy2) - cy1

    new_w = clipped_x2 - clipped_x1
    new_h = clipped_y2 - clipped_y1

    if new_w < 2 or new_h < 2:
        return None

    # Re-normalise to crop dimensions
    return {
        "x":      clipped_x1 / crop_w,
        "y":      clipped_y1 / crop_h,
        "width":  new_w / crop_w,
        "height": new_h / crop_h,
        # Preserve extra fields (confidence, class_name, etc.)
        **{k: v for k, v in region.items() if k not in ("x", "y", "width", "height")},
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *val* into [lo, hi]."""
    return max(lo, min(hi, val))


def _normalised_to_pixel(
    x: float, y: float, w: float, h: float,
    img_w: int, img_h: int,
) -> tuple[int, int, int, int]:
    """Convert normalised (0-1) coords → pixel coords (x1, y1, x2, y2)."""
    x = _clamp(x)
    y = _clamp(y)
    w = _clamp(w, 0.0, 1.0 - x)
    h = _clamp(h, 0.0, 1.0 - y)
    x1 = int(x * img_w)
    y1 = int(y * img_h)
    x2 = int((x + w) * img_w)
    y2 = int((y + h) * img_h)
    return x1, y1, x2, y2


def _draw_corner_brackets(
    img: np.ndarray,
    x1: int, y1: int, x2: int, y2: int,
    color: tuple[int, int, int],
    thickness: int = 2,
    corner_len: int | None = None,
) -> None:
    """
    Draw corner brackets (L-shaped) instead of full rectangles.
    Looks more modern and clinical than solid rectangles.
    """
    box_w = x2 - x1
    box_h = y2 - y1
    if corner_len is None:
        corner_len = max(8, min(int(min(box_w, box_h) * _CORNER_LEN_RATIO), 40))

    # Top-left
    cv2.line(img, (x1, y1), (x1 + corner_len, y1), color, thickness)
    cv2.line(img, (x1, y1), (x1, y1 + corner_len), color, thickness)
    # Top-right
    cv2.line(img, (x2, y1), (x2 - corner_len, y1), color, thickness)
    cv2.line(img, (x2, y1), (x2, y1 + corner_len), color, thickness)
    # Bottom-left
    cv2.line(img, (x1, y2), (x1 + corner_len, y2), color, thickness)
    cv2.line(img, (x1, y2), (x1, y2 - corner_len), color, thickness)
    # Bottom-right
    cv2.line(img, (x2, y2), (x2 - corner_len, y2), color, thickness)
    cv2.line(img, (x2, y2), (x2, y2 - corner_len), color, thickness)


def _draw_label(
    img: np.ndarray,
    text: str,
    x1: int, y1: int,
    color: tuple[int, int, int],
) -> None:
    """Draw a label with a dark background pill above the bounding box."""
    (tw, th), baseline = cv2.getTextSize(
        text, _LABEL_FONT, _LABEL_SCALE, _LABEL_THICK,
    )
    pad = 4
    label_y = max(y1 - 6, th + pad + 2)

    # Rounded-rect background
    bg_x1 = x1
    bg_y1 = label_y - th - pad
    bg_x2 = x1 + tw + pad * 2
    bg_y2 = label_y + pad // 2

    # Dark semi-transparent pill
    overlay = img.copy()
    cv2.rectangle(overlay, (bg_x1, bg_y1), (bg_x2, bg_y2), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.75, img, 0.25, 0, img)

    cv2.putText(
        img, text, (x1 + pad, label_y - 1),
        _LABEL_FONT, _LABEL_SCALE, color, _LABEL_THICK, cv2.LINE_AA,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def generate_condition_overlay(
    image_bytes: bytes,
    condition_name: str,
    regions: list[dict],
    label: bool = True,
) -> bytes:
    """
    Draw condition-specific annotations on a **zoomed face crop**.

    Parameters
    ----------
    image_bytes : bytes
        Raw JPEG/PNG image bytes.
    condition_name : str
        The detected condition name (e.g. ``"Acne / Pimples"``).
    regions : list[dict]
        Normalised bounding boxes, each with keys ``x, y, width, height``
        in the 0.0–1.0 range (relative to the full original image).
    label : bool
        Whether to render the condition name above each region.

    Returns
    -------
    bytes
        JPEG-encoded annotated face-cropped image. If no valid regions are
        found the original *image_bytes* are returned unchanged.
    """
    # Decode image
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        logger.error("Failed to decode image for overlay generation")
        return image_bytes

    orig_h, orig_w = img.shape[:2]

    # ── Step 1: Detect face and crop ─────────────────────────────────────
    cropped, crop_box, keypoints = _detect_and_crop_face(img)
    crop_h, crop_w = cropped.shape[:2]

    cond_lower = condition_name.lower().strip()

    # Auto-correct spatial offset for dark circles using eye keypoints if available
    if cond_lower in ("dark circles", "dark_circles") and len(keypoints) >= 2:
        le_x = keypoints[0]["x"]
        le_y = keypoints[0]["y"]
        re_x = keypoints[1]["x"]
        re_y = keypoints[1]["y"]
        
        # Position the box directly under the eye level line with a slightly raised 0.002 offset
        eye_y = (le_y + re_y) / 2.0
        corrected_y = eye_y + 0.002
        
        for region in regions:
            region["y"] = corrected_y
            region["height"] = min(0.040, region.get("height", 0.15))
            
            # Align horizontally: center the box under the eyes if it was shifted
            rx = float(region.get("x", 0))
            rw = float(region.get("width", 0))
            rcx = rx + rw / 2.0
            
            if rw > 0.25:
                # Spans both eyes, center it
                target_cx = (le_x + re_x) / 2.0
            elif abs(rcx - le_x) < abs(rcx - re_x):
                # Closer to left eye
                target_cx = le_x
            else:
                # Closer to right eye
                target_cx = re_x
                
            region["x"] = target_cx - rw / 2.0
            logger.info("Auto-corrected 'dark_circles' region: x=%.4f, y=%.4f", region["x"], region["y"])

    # ── Step 2: Remap regions to crop space ──────────────────────────────
    if crop_box is not None:
        remapped_regions = []
        for region in regions:
            remapped = _remap_region_to_crop(region, orig_w, orig_h, crop_box)
            if remapped is not None:
                remapped_regions.append(remapped)
    else:
        # No face detected — use regions as-is on full image
        remapped_regions = regions

    cond_lower = condition_name.lower().strip()
    cond_key   = cond_lower.replace(" ", "_").replace("-", "_")

    # Normalize color map and fill conditions keys
    normalized_colors = {k.replace(" ", "_").replace("-", "_"): v for k, v in CONDITION_COLORS.items()}
    normalized_fill   = {k.replace(" ", "_").replace("-", "_") for k in _FILL_CONDITIONS}

    color    = normalized_colors.get(cond_key, _DEFAULT_COLOR)
    use_fill = cond_key in normalized_fill

    valid_count = 0
    for region in remapped_regions:
        # ── Parse & validate ─────────────────────────────────────────────
        try:
            rx = float(region.get("x", 0))
            ry = float(region.get("y", 0))
            rw = float(region.get("width", 0))
            rh = float(region.get("height", 0))
        except (TypeError, ValueError):
            logger.warning("Skipping malformed region: %s", region)
            continue

        if rw < 0.01 or rh < 0.01:
            continue  # too small to be meaningful

        # Skip oversized boxes (>50% of the crop in either dimension)
        if rw > 0.50 and rh > 0.50:
            logger.info(
                "Skipping oversized region for '%s': %.2f x %.2f",
                condition_name, rw, rh,
            )
            continue

        x1, y1, x2, y2 = _normalised_to_pixel(rx, ry, rw, rh, crop_w, crop_h)

        # ── Draw ─────────────────────────────────────────────────────────
        if use_fill:
            overlay = cropped.copy()
            cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            cv2.addWeighted(overlay, _FILL_ALPHA, cropped, 1 - _FILL_ALPHA, 0, cropped)
            # Thin border + corner brackets
            cv2.rectangle(cropped, (x1, y1), (x2, y2), color, 1)
        else:
            # Draw rectangular line border for localized conditions
            cv2.rectangle(cropped, (x1, y1), (x2, y2), color, _RECT_THICKNESS)

        # ── Label ────────────────────────────────────────────────────────
        if label:
            conf = region.get("confidence")
            if conf is not None:
                label_text = f"{condition_name.upper()} {int(conf * 100)}%"
            else:
                label_text = condition_name.upper()
            _draw_label(cropped, label_text, x1, y1, color)

        valid_count += 1

    if valid_count == 0:
        logger.info("No valid regions for '%s' — returning original image", condition_name)
        return image_bytes

    # ── Encode ────────────────────────────────────────────────────────────
    ok, buf = cv2.imencode(".jpg", cropped, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        logger.error("Failed to encode overlay image for '%s'", condition_name)
        return image_bytes

    logger.info(
        "Generated zoomed overlay for '%s' with %d region(s) (cropped: %dx%d)",
        condition_name, valid_count, crop_w, crop_h,
    )
    return buf.tobytes()


def generate_redness_overlay(
    image_bytes: bytes,
    regions: list[dict],
    redness_score: int = 0,
) -> bytes:
    """
    Special overlay for visible redness.

    Uses :func:`generate_condition_overlay` with the "Redness / Irritation"
    style (semi-transparent red fill), then stamps the redness score as a
    watermark in the top-right corner.

    Parameters
    ----------
    image_bytes : bytes
        Raw JPEG/PNG image bytes.
    regions : list[dict]
        Normalised bounding boxes for redness regions.
    redness_score : int
        0-100 redness intensity score.

    Returns
    -------
    bytes
        JPEG-encoded annotated image.
    """
    result = generate_condition_overlay(
        image_bytes, "Redness / Irritation", regions, label=True,
    )

    # Stamp score watermark in the top-right corner
    if redness_score > 0:
        arr = np.frombuffer(result, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            score_text = f"Redness: {redness_score}/100"
            (tw, th), _ = cv2.getTextSize(score_text, _LABEL_FONT, 0.5, 1)
            x = img.shape[1] - tw - 10
            y = th + 10

            # Dark pill background
            overlay = img.copy()
            cv2.rectangle(
                overlay, (x - 4, y - th - 4), (x + tw + 4, y + 4), (30, 30, 30), -1,
            )
            cv2.addWeighted(overlay, 0.75, img, 0.25, 0, img)

            cv2.putText(
                img, score_text, (x, y),
                _LABEL_FONT, 0.5, (0, 0, 255), 1, cv2.LINE_AA,
            )
            ok, buf = cv2.imencode(
                ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
            )
            if ok:
                return buf.tobytes()

    return result
