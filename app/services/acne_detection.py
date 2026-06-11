# app/services/acne_detection.py
"""
Local Skin Lesion Detection service using YOLOv8 with MediaPipe Tasks Face Detection,
SAHI (Sliced Aided Hyper Inference) patch-based logic, and Multi-Model Ensembling.
"""

from __future__ import annotations

import logging
import os
import cv2
import numpy as np

from app.utils.face_detection_utils import detect_largest_face

logger = logging.getLogger(__name__)

# Lazy loading of YOLO models
_acne_model = None
_lesion_model = None

_FACE_PAD_RATIO = 0.20  # 20% padding around detected face


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


# ── NMS (Non-Maximum Suppression) & Bounding Box Utilities ──────────────────

def _calculate_iou(box1: dict, box2: dict) -> float:
    """Calculate Intersection over Union (IoU) of two normalized bounding boxes."""
    x1_min, y1_min = box1["x"], box1["y"]
    x1_max, y1_max = x1_min + box1["width"], y1_min + box1["height"]
    
    x2_min, y2_min = box2["x"], box2["y"]
    x2_max, y2_max = x2_min + box2["width"], y2_min + box2["height"]
    
    # Intersection
    inter_x_min = max(x1_min, x2_min)
    inter_y_min = max(y1_min, y2_min)
    inter_x_max = min(x1_max, x2_max)
    inter_y_max = min(y1_max, y2_max)
    
    inter_w = max(0.0, inter_x_max - inter_x_min)
    inter_h = max(0.0, inter_y_max - inter_y_min)
    inter_area = inter_w * inter_h
    
    if inter_area == 0.0:
        return 0.0
        
    # Union
    area1 = box1["width"] * box1["height"]
    area2 = box2["width"] * box2["height"]
    union_area = area1 + area2 - inter_area
    
    return inter_area / union_area if union_area > 0.0 else 0.0


def apply_nms(boxes: list[dict], iou_threshold: float = 0.40) -> list[dict]:
    """Apply category-agnostic Non-Maximum Suppression (NMS) to clear overlapping duplicates."""
    if not boxes:
        return []
    
    # Sort by confidence descending
    sorted_boxes = sorted(boxes, key=lambda b: b.get("confidence", 0.0), reverse=True)
    
    keep = []
    while sorted_boxes:
        best = sorted_boxes.pop(0)
        keep.append(best)
        
        remaining = []
        for box in sorted_boxes:
            if _calculate_iou(best, box) < iou_threshold:
                remaining.append(box)
        sorted_boxes = remaining
        
    return keep


# ── YOLO Inference Logic ─────────────────────────────────────────────────────

def _run_yolo_on_image(model, img: np.ndarray, conf_threshold: float = 0.20) -> list[dict]:
    """Run a YOLO model on a single BGR image. Returns normalized detections in image space."""
    results = model(img, conf=conf_threshold, verbose=False)
    detections: list[dict] = []
    
    for result in results:
        boxes = result.boxes
        for box in boxes:
            xyxyn = box.xyxyn[0].tolist()
            xmin, ymin, xmax, ymax = xyxyn
            
            w = xmax - xmin
            h = ymax - ymin
            
            if w < 0.01 or h < 0.01:
                continue
                
            conf = float(box.conf[0]) if box.conf is not None else 0.0
            cls_id = int(box.cls[0]) if box.cls is not None else None
            cls_name = model.names.get(cls_id, "unknown") if cls_id is not None else "unknown"
            
            detections.append({
                "x": xmin,
                "y": ymin,
                "width": w,
                "height": h,
                "confidence": conf,
                "class_name": cls_name
            })
            
    return detections


def _run_sahi_inference(model, crop_img: np.ndarray, slice_size: int = 320, overlap: float = 0.20, conf_threshold: float = 0.20) -> list[dict]:
    """Run sliced (sliding window) inference to capture micro-lesions at higher resolutions using batched YOLO inference."""
    img_h, img_w = crop_img.shape[:2]
    
    # If the image is too small to slice, just run normal inference
    if img_h <= slice_size or img_w <= slice_size:
        return _run_yolo_on_image(model, crop_img, conf_threshold)
        
    step_x = int(slice_size * (1.0 - overlap))
    step_y = int(slice_size * (1.0 - overlap))
    
    # Generate coordinates for sliding windows
    x_starts = list(range(0, img_w - slice_size + 1, step_x))
    if x_starts[-1] + slice_size < img_w:
        x_starts.append(img_w - slice_size)
    elif x_starts[-1] + slice_size > img_w:
        x_starts[-1] = img_w - slice_size
        
    y_starts = list(range(0, img_h - slice_size + 1, step_y))
    if y_starts[-1] + slice_size < img_h:
        y_starts.append(img_h - slice_size)
    elif y_starts[-1] + slice_size > img_h:
        y_starts[-1] = img_h - slice_size
        
    x_starts = sorted(list(set(x_starts)))
    y_starts = sorted(list(set(y_starts)))
    
    slices = []
    slice_coords = []
    
    for ys in y_starts:
        for xs in x_starts:
            slice_img = crop_img[ys:ys+slice_size, xs:xs+slice_size]
            slices.append(slice_img)
            slice_coords.append((xs, ys))
            
    if not slices:
        return []
        
    # Run batched inference on all slices in parallel
    results = model(slices, conf=conf_threshold, verbose=False)
    
    all_detections = []
    
    for idx, result in enumerate(results):
        xs, ys = slice_coords[idx]
        boxes = result.boxes
        for box in boxes:
            xyxyn = box.xyxyn[0].tolist()
            xmin, ymin, xmax, ymax = xyxyn
            
            w = xmax - xmin
            h = ymax - ymin
            
            if w < 0.01 or h < 0.01:
                continue
                
            conf = float(box.conf[0]) if box.conf is not None else 0.0
            cls_id = int(box.cls[0]) if box.cls is not None else None
            cls_name = model.names.get(cls_id, "unknown") if cls_id is not None else "unknown"
            
            px_x = xs + xmin * slice_size
            px_y = ys + ymin * slice_size
            px_w = w * slice_size
            px_h = h * slice_size
            
            all_detections.append({
                "x": px_x / img_w,
                "y": px_y / img_h,
                "width": px_w / img_w,
                "height": px_h / img_h,
                "confidence": conf,
                "class_name": cls_name
            })
            
    return all_detections


def run_hybrid_sahi_inference(model, crop_img: np.ndarray, conf_threshold: float = 0.20, iou_threshold: float = 0.40, use_sahi: bool = True) -> list[dict]:
    """Run full image inference and sliced inference combined, filtering oversized boxes."""
    # 1. Full image crop inference
    full_detections = _run_yolo_on_image(model, crop_img, conf_threshold)
    
    # 2. Sliced window inference
    if use_sahi:
        sliced_detections = _run_sahi_inference(model, crop_img, slice_size=320, overlap=0.20, conf_threshold=conf_threshold)
        merged = full_detections + sliced_detections
    else:
        merged = full_detections
    
    # Filter out oversized boxes (>40% of face in both dimensions)
    filtered = []
    for det in merged:
        if det["width"] > 0.40 and det["height"] > 0.40:
            continue
        filtered.append(det)
        
    return apply_nms(filtered, iou_threshold)


# ── Core Pipeline orchestrator (Ensembling & Face Cropping) ──────────────────

def _run_ensembled_detection(image_bytes: bytes, target_type: str, use_sahi: bool = True) -> list[dict]:
    """
    Run the ensembled YOLO inference pipeline with Face Crop, SAHI, and hyperparameter tuning.
    
    Steps:
      1. Decode full image.
      2. Detect face and crop to face region + padding.
      3. Run hybrid SAHI inference on both models.
      4. Filter, blend, and merge predictions.
      5. Remap detections back to full-image normalized coordinates.
    """
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        full_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if full_img is None:
            logger.warning("Failed to decode image bytes.")
            return []

        full_h, full_w = full_img.shape[:2]

        # ── Step 1: Detect face and crop ─────────────────────────────────
        face_box = detect_largest_face(full_img)
        crop_box = None
        if face_box is not None:
            fx, fy, fw, fh = face_box
            pad_x = int(fw * _FACE_PAD_RATIO)
            pad_y = int(fh * _FACE_PAD_RATIO)
            cx1 = max(0, fx - pad_x)
            cy1 = max(0, fy - pad_y)
            cx2 = min(full_w, fx + fw + pad_x)
            cy2 = min(full_h, fy + fh + pad_y)
            crop_img = full_img[cy1:cy2, cx1:cx2].copy()
            crop_box = (cx1, cy1, cx2, cy2)
            crop_h, crop_w = crop_img.shape[:2]
        else:
            crop_img = full_img
            crop_h, crop_w = full_h, full_w

        # ── Step 2: Run models ───────────────────────────────────────────
        raw_detections: list[dict] = []
        
        # Load models lazily
        acne_model = get_acne_detector()
        lesion_model = get_lesion_detector()
        
        if target_type == "acne":
            # Running acne model (conf=0.18, iou=0.40)
            acne_dets = run_hybrid_sahi_inference(acne_model, crop_img, conf_threshold=0.18, iou_threshold=0.40, use_sahi=use_sahi)
            raw_detections.extend(acne_dets)
            
            # Running general lesion model (conf=0.22, iou=0.40)
            lesion_dets = run_hybrid_sahi_inference(lesion_model, crop_img, conf_threshold=0.22, iou_threshold=0.40, use_sahi=use_sahi)
            
            # Filter lesion model detections: only keep active inflammatory acne classes (papule, pustule, nodule)
            acne_classes = {"papule", "pustule", "nodule"}
            for det in lesion_dets:
                if det["class_name"] in acne_classes:
                    # Rename classification to 'acne' for unified handling
                    det["class_name"] = "acne"
                    raw_detections.append(det)
                    
        else:  # target_type == "lesion"
            # Running general lesion model (conf=0.20, iou=0.40)
            lesion_dets = run_hybrid_sahi_inference(lesion_model, crop_img, conf_threshold=0.20, iou_threshold=0.40, use_sahi=use_sahi)
            raw_detections.extend(lesion_dets)
            
            # Running acne model (conf=0.18, iou=0.40)
            acne_dets = run_hybrid_sahi_inference(acne_model, crop_img, conf_threshold=0.18, iou_threshold=0.40, use_sahi=use_sahi)
            for det in acne_dets:
                # acne is also a lesion, so add it
                raw_detections.append(det)

        # ── Step 3: Run final NMS across all compiled detections ─────────
        final_crop_dets = apply_nms(raw_detections, iou_threshold=0.40)

        # ── Step 4: Remap back to full-image normalized coordinates ──────
        final_regions = []
        for det in final_crop_dets:
            x_c = det["x"]
            y_c = det["y"]
            w_c = det["width"]
            h_c = det["height"]
            
            if crop_box is not None:
                cx1, cy1, cx2, cy2 = crop_box
                px1 = cx1 + x_c * crop_w
                py1 = cy1 + y_c * crop_h
                px2 = cx1 + (x_c + w_c) * crop_w
                py2 = cy1 + (y_c + h_c) * crop_h
                
                x_full = px1 / full_w
                y_full = py1 / full_h
                w_full = (px2 - px1) / full_w
                h_full = (py2 - py1) / full_h
            else:
                x_full = x_c
                y_full = y_c
                w_full = w_c
                h_full = h_c
                
            region = {
                "x": round(x_full, 4),
                "y": round(y_full, 4),
                "width": round(w_full, 4),
                "height": round(h_full, 4),
                "confidence": round(det["confidence"], 3),
                "class_name": det["class_name"]
            }
            final_regions.append(region)

        logger.info(
            "Ensembled %s detector found %d region(s) (face crop: %s)",
            target_type, len(final_regions),
            f"{crop_w}x{crop_h}" if crop_box else "full image",
        )
        return final_regions

    except Exception as exc:
        logger.error("Error in ensembled %s detection: %s", target_type, exc)
        return []


def detect_acne_regions(image_bytes: bytes, use_sahi: bool = True) -> list[dict]:
    """Run ensembled face-cropped YOLO pipeline for acne detection."""
    return _run_ensembled_detection(image_bytes, "acne", use_sahi=use_sahi)


def detect_lesion_regions(image_bytes: bytes, use_sahi: bool = True) -> list[dict]:
    """Run ensembled face-cropped YOLO pipeline for general skin lesion detection."""
    return _run_ensembled_detection(image_bytes, "lesion", use_sahi=use_sahi)


def detect_face_regions_combined(image_bytes: bytes, use_sahi: bool = True) -> tuple[list[dict], list[dict]]:
    """
    Run the ensembled YOLO inference pipeline once for BOTH acne and general lesions.
    This avoids running the same models multiple times on the same image.
    """
    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        full_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if full_img is None:
            logger.warning("Failed to decode image bytes.")
            return [], []

        full_h, full_w = full_img.shape[:2]

        # ── Step 1: Detect face and crop ─────────────────────────────────
        face_box = detect_largest_face(full_img)
        crop_box = None
        if face_box is not None:
            fx, fy, fw, fh = face_box
            pad_x = int(fw * _FACE_PAD_RATIO)
            pad_y = int(fh * _FACE_PAD_RATIO)
            cx1 = max(0, fx - pad_x)
            cy1 = max(0, fy - pad_y)
            cx2 = min(full_w, fx + fw + pad_x)
            cy2 = min(full_h, fy + fh + pad_y)
            crop_img = full_img[cy1:cy2, cx1:cx2].copy()
            crop_box = (cx1, cy1, cx2, cy2)
            crop_h, crop_w = crop_img.shape[:2]
        else:
            crop_img = full_img
            crop_h, crop_w = full_h, full_w

        # ── Step 2: Load models lazily ───────────────────────────────────
        acne_model = get_acne_detector()
        lesion_model = get_lesion_detector()

        # Run each model exactly once!
        # Acne model (conf=0.18, iou=0.40)
        acne_dets = run_hybrid_sahi_inference(acne_model, crop_img, conf_threshold=0.18, iou_threshold=0.40, use_sahi=use_sahi)
        
        # Lesion model (conf=0.20, iou=0.40)
        lesion_dets = run_hybrid_sahi_inference(lesion_model, crop_img, conf_threshold=0.20, iou_threshold=0.40, use_sahi=use_sahi)

        # ── Step 3: Build acne and lesion raw detection lists ───────────
        # Build Acne Raw
        acne_raw = list(acne_dets)
        acne_classes = {"papule", "pustule", "nodule"}
        for det in lesion_dets:
            if det["class_name"] in acne_classes:
                det_copy = dict(det)
                det_copy["class_name"] = "acne"
                acne_raw.append(det_copy)

        # Build Lesion Raw
        lesion_raw = list(lesion_dets) + list(acne_dets)

        # Apply NMS
        final_acne_crop = apply_nms(acne_raw, iou_threshold=0.40)
        final_lesion_crop = apply_nms(lesion_raw, iou_threshold=0.40)

        # ── Step 4: Remap back to full-image normalized coordinates ──────
        def remap_regions(crop_dets):
            final_regions = []
            for det in crop_dets:
                x_c = det["x"]
                y_c = det["y"]
                w_c = det["width"]
                h_c = det["height"]
                
                if crop_box is not None:
                    cx1, cy1, cx2, cy2 = crop_box
                    px1 = cx1 + x_c * crop_w
                    py1 = cy1 + y_c * crop_h
                    px2 = cx1 + (x_c + w_c) * crop_w
                    py2 = cy1 + (y_c + h_c) * crop_h
                    
                    x_full = px1 / full_w
                    y_full = py1 / full_h
                    w_full = (px2 - px1) / full_w
                    h_full = (py2 - py1) / full_h
                else:
                    x_full = x_c
                    y_full = y_c
                    w_full = w_c
                    h_full = h_c
                    
                region = {
                    "x": round(x_full, 4),
                    "y": round(y_full, 4),
                    "width": round(w_full, 4),
                    "height": round(h_full, 4),
                    "confidence": round(det["confidence"], 3),
                    "class_name": det["class_name"]
                }
                final_regions.append(region)
            return final_regions

        acne_regions = remap_regions(final_acne_crop)
        lesion_regions = remap_regions(final_lesion_crop)

        logger.info(
            "Ensembled combined detector found %d acne and %d lesion region(s) (face crop: %s)",
            len(acne_regions), len(lesion_regions),
            f"{crop_w}x{crop_h}" if crop_box else "full image",
        )
        return acne_regions, lesion_regions

    except Exception as exc:
        logger.error("Error in ensembled combined detection: %s", exc)
        return [], []

