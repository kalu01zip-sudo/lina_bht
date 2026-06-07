# app/utils/dataset_exporter.py
"""
Utility script to export cropped face training datasets from database scans.
Downloads images, crops to the face region, and generates YOLO-format annotations.
"""

import os
import logging
import cv2
import numpy as np
import httpx
from pymongo import MongoClient
from app.utils.face_detection_utils import detect_largest_face

logger = logging.getLogger(__name__)

# YOLO Class mappings
CLASS_MAP = {
    "comedone": 0,
    "papule": 1,
    "pustule": 2,
    "nodule": 3,
    "macule": 4,
    "patch": 5,
    "acne": 6,
    "acne / pimples": 6,
    "blackheads": 0,
    "whiteheads": 0,
}

def export_dataset(mongo_url: str, db_name: str, output_dir: str = "dataset_export"):
    """
    Query all face scans from MongoDB, download images, crop to face,
    and save YOLO format dataset (images & labels).
    """
    client = MongoClient(mongo_url)
    db = client[db_name]
    scans_col = db["face_scans"]
    
    os.makedirs(os.path.join(output_dir, "images"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "labels"), exist_ok=True)
    
    logger.info("Starting dataset export from MongoDB...")
    
    cursor = scans_col.find({"analysis.detected_condition": {"$exists": True}})
    count = 0
    
    with httpx.Client(timeout=30.0) as http_client:
        for idx, scan in enumerate(cursor):
            scan_id = str(scan["_id"])
            images = scan.get("images", [])
            if not images:
                continue
                
            # Usually the first image is the frontal face
            img_url = images[0]
            
            # Retrieve detections
            conditions = scan["analysis"].get("detected_condition", [])
            regions_to_export = []
            for cond in conditions:
                cond_name = cond.get("name", "").lower().strip()
                class_id = CLASS_MAP.get(cond_name)
                if class_id is None:
                    continue
                    
                for reg in cond.get("regions", []):
                    regions_to_export.append({
                        "class_id": class_id,
                        "x": reg.get("x", 0.0),
                        "y": reg.get("y", 0.0),
                        "width": reg.get("width", 0.0),
                        "height": reg.get("height", 0.0),
                    })
                    
            if not regions_to_export:
                continue
                
            # Download image
            try:
                r = http_client.get(img_url)
                if r.status_code != 200:
                    logger.warning("Failed to download image %s", img_url)
                    continue
                arr = np.frombuffer(r.content, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    continue
            except Exception as e:
                logger.error("Error downloading image: %s", e)
                continue
                
            img_h, img_w = img.shape[:2]
            
            # Detect face crop box
            face_box = detect_largest_face(img)
            if face_box is None:
                # Fallback to full image if no face detected
                crop_box = (0, 0, img_w, img_h)
            else:
                fx, fy, fw, fh = face_box
                # Expand crop with 20% padding
                pad_x = int(fw * 0.20)
                pad_y = int(fh * 0.20)
                cx1 = max(0, fx - pad_x)
                cy1 = max(0, fy - pad_y)
                cx2 = min(img_w, fx + fw + pad_x)
                cy2 = min(img_h, fy + fh + pad_y)
                crop_box = (cx1, cy1, cx2, cy2)
                
            cx1, cy1, cx2, cy2 = crop_box
            crop_w = cx2 - cx1
            crop_h = cy2 - cy1
            
            if crop_w <= 0 or crop_h <= 0:
                continue
                
            cropped_img = img[cy1:cy2, cx1:cx2]
            
            # Remap annotations to crop space
            yolo_labels = []
            for reg in regions_to_export:
                rx, ry, rw, rh = reg["x"], reg["y"], reg["width"], reg["height"]
                
                # Convert normalized full-image -> pixel coords
                px1 = rx * img_w
                py1 = ry * img_h
                px2 = (rx + rw) * img_w
                py2 = (ry + rh) * img_h
                
                # Intersect with crop box
                ix1 = max(px1, cx1) - cx1
                iy1 = max(py1, cy1) - cy1
                ix2 = min(px2, cx2) - cx1
                iy2 = min(py2, cy2) - cy1
                
                iw = ix2 - ix1
                ih = iy2 - iy1
                
                if iw < 2 or ih < 2:
                    continue
                    
                # Convert to YOLO center format normalized to crop dimensions:
                # class_id center_x center_y width height
                center_x = (ix1 + iw / 2) / crop_w
                center_y = (iy1 + ih / 2) / crop_h
                norm_w = iw / crop_w
                norm_h = ih / crop_h
                
                yolo_labels.append(f"{reg['class_id']} {center_x:.6f} {center_y:.6f} {norm_w:.6f} {norm_h:.6f}")
                
            if not yolo_labels:
                continue
                
            # Write files
            base_name = f"scan_{scan_id}"
            img_path = os.path.join(output_dir, "images", f"{base_name}.jpg")
            label_path = os.path.join(output_dir, "labels", f"{base_name}.txt")
            
            cv2.imwrite(img_path, cropped_img)
            with open(label_path, "w") as f:
                f.write("\n".join(yolo_labels) + "\n")
                
            count += 1
            
    logger.info("Successfully exported %d face-cropped dataset samples to '%s'.", count, output_dir)
    return count

if __name__ == "__main__":
    import sys
    # Extract params from environment or run with default local settings
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "skinsense")
    export_dataset(mongo_url, db_name)
