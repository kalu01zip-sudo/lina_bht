# app/utils/overlay_utils.py
"""
Overlay image generation for face scan conditions.

Uses OpenCV to draw condition-specific annotations (rectangles, semi-transparent
fills, labels) on face images based on normalised bounding-box coordinates
returned by Claude Vision.

Two overlay styles:
  - Rectangle borders  → localized conditions (acne, blackheads, pores …)
  - Semi-transparent fill → diffuse conditions (redness, oiliness, dullness …)

All 16 trigger types from the scan prompt have a dedicated colour.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

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
_RECT_THICKNESS = 5
_FILL_ALPHA     = 0.35
_LABEL_FONT     = cv2.FONT_HERSHEY_SIMPLEX
_LABEL_SCALE    = 1
_LABEL_THICK    = 1
_JPEG_QUALITY   = 85


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


# ── Public API ────────────────────────────────────────────────────────────────


def generate_condition_overlay(
    image_bytes: bytes,
    condition_name: str,
    regions: list[dict],
    label: bool = True,
) -> bytes:
    """
    Draw condition-specific annotations on a face image.

    Parameters
    ----------
    image_bytes : bytes
        Raw JPEG/PNG image bytes.
    condition_name : str
        The detected condition name (e.g. ``"Acne / Pimples"``).
    regions : list[dict]
        Normalised bounding boxes, each with keys ``x, y, width, height``
        in the 0.0–1.0 range.
    label : bool
        Whether to render the condition name above each region.

    Returns
    -------
    bytes
        JPEG-encoded annotated image.  If no valid regions are found the
        original *image_bytes* are returned unchanged.
    """
    # Decode image
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        logger.error("Failed to decode image for overlay generation")
        return image_bytes

    img_h, img_w = img.shape[:2]
    cond_lower = condition_name.lower().strip()
    cond_key   = cond_lower.replace(" ", "_").replace("-", "_")

    # Normalize color map and fill conditions keys to match normalized names (spaces/dashes mapped to underscores)
    normalized_colors = {k.replace(" ", "_").replace("-", "_"): v for k, v in CONDITION_COLORS.items()}
    normalized_fill   = {k.replace(" ", "_").replace("-", "_") for k in _FILL_CONDITIONS}

    color    = normalized_colors.get(cond_key, _DEFAULT_COLOR)
    use_fill = cond_key in normalized_fill

    valid_count = 0
    for region in regions:
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

        x1, y1, x2, y2 = _normalised_to_pixel(rx, ry, rw, rh, img_w, img_h)

        # ── Draw ─────────────────────────────────────────────────────────
        if use_fill:
            overlay = img.copy()
            cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            cv2.addWeighted(overlay, _FILL_ALPHA, img, 1 - _FILL_ALPHA, 0, img)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)   # thin border
        else:
            cv2.rectangle(img, (x1, y1), (x2, y2), color, _RECT_THICKNESS)

        # ── Label ────────────────────────────────────────────────────────
        if label:
            label_text = condition_name.upper()
            (tw, th), _ = cv2.getTextSize(
                label_text, _LABEL_FONT, _LABEL_SCALE, _LABEL_THICK,
            )
            label_y = max(y1 - 6, th + 4)
            # Dark background so the label is readable on any skin tone
            cv2.rectangle(
                img,
                (x1, label_y - th - 4),
                (x1 + tw + 4, label_y + 2),
                (0, 0, 0), -1,
            )
            cv2.putText(
                img, label_text, (x1 + 2, label_y - 2),
                _LABEL_FONT, _LABEL_SCALE, color, _LABEL_THICK, cv2.LINE_AA,
            )

        valid_count += 1

    if valid_count == 0:
        logger.info("No valid regions for '%s' — returning original image", condition_name)
        return image_bytes

    # ── Encode ────────────────────────────────────────────────────────────
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        logger.error("Failed to encode overlay image for '%s'", condition_name)
        return image_bytes

    logger.info(
        "Generated overlay for '%s' with %d region(s)", condition_name, valid_count,
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
            (tw, th), _ = cv2.getTextSize(score_text, _LABEL_FONT, 0.7, 2)
            x = img.shape[1] - tw - 12
            y = th + 12
            cv2.rectangle(
                img, (x - 4, y - th - 4), (x + tw + 4, y + 6), (0, 0, 0), -1,
            )
            cv2.putText(
                img, score_text, (x, y),
                _LABEL_FONT, 0.7, (0, 0, 255), 2, cv2.LINE_AA,
            )
            ok, buf = cv2.imencode(
                ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
            )
            if ok:
                return buf.tobytes()

    return result
