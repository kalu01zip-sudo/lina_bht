# app/services/acne_detection.py
"""
Local Skin Lesion Detection service using YOLOv8.

Two models:
  - `acne_detector_best.pt`  → acne-specific lesion detection
  - `lesion_yolov8_best.pt`  → general skin lesion detection
    (comedones, papules, pustules, nodules — covers blackheads, whiteheads, etc.)
"""

from __future__ import annotations

import logging
import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Lazy loading of YOLO models
_acne_model = None
_lesion_model = None


def get_acne_detector():
    """Lazily load the YOLOv8 acne-specific model."""
    global _acne_model
    if _acne_model is None:
        try:
            from ultralytics import YOLO
            model_path = "app/models/acne_detector_best.pt"
            _acne_model = YOLO(model_path)
            logger.info("YOLOv8 acne detector model loaded successfully.")
        except Exception as exc:
            logger.error("Failed to load YOLOv8 acne model: %s", exc)
            raise
    return _acne_model


def get_lesion_detector():
    """Lazily load the YOLOv8 general lesion model."""
    global _lesion_model
    if _lesion_model is None:
        try:
            from ultralytics import YOLO
            model_path = "app/models/lesion_yolov8_best.pt"
            _lesion_model = YOLO(model_path)
            logger.info("YOLOv8 general lesion detector model loaded successfully.")
        except Exception as exc:
            logger.error("Failed to load YOLOv8 lesion model: %s", exc)
            raise
    return _lesion_model


def _run_yolo_detection(image_bytes: bytes, model_getter, model_name: str) -> list[dict]:
    """
    Shared helper: run a YOLO model on image bytes and return normalised regions.

    Returns
    -------
    list[dict]
        [{"x": xmin, "y": ymin, "width": w, "height": h}, ...]
    """
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            logger.warning("Failed to decode image bytes for %s.", model_name)
            return []

        model = model_getter()
        results = model(img, verbose=False)

        regions: list[dict] = []
        for result in results:
            boxes = result.boxes
            for box in boxes:
                xyxyn = box.xyxyn[0].tolist()
                xmin, ymin, xmax, ymax = xyxyn

                w = xmax - xmin
                h = ymax - ymin

                if w < 0.01 or h < 0.01:
                    continue

                regions.append({
                    "x": round(xmin, 4),
                    "y": round(ymin, 4),
                    "width": round(w, 4),
                    "height": round(h, 4),
                })

        logger.info("%s found %d region(s)", model_name, len(regions))
        return regions

    except Exception as exc:
        logger.error("Error running %s: %s", model_name, exc)
        return []


def detect_acne_regions(image_bytes: bytes) -> list[dict]:
    """Run the acne-specific YOLOv8 model. Returns normalised bounding boxes."""
    return _run_yolo_detection(image_bytes, get_acne_detector, "acne detector")


def detect_lesion_regions(image_bytes: bytes) -> list[dict]:
    """Run the general lesion YOLOv8 model. Returns normalised bounding boxes."""
    return _run_yolo_detection(image_bytes, get_lesion_detector, "lesion detector")
