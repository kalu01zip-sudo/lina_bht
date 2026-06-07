# app/utils/face_detection_utils.py
import logging
import os
import cv2
import numpy as np
from app.utils.model_downloader import ensure_face_detector_model

logger = logging.getLogger(__name__)

# Lazy loaded detectors
_mp_detector = None
_haar_cascade = None

def _get_mp_detector():
    global _mp_detector
    if _mp_detector is not None:
        return _mp_detector
    
    try:
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        
        model_path = ensure_face_detector_model()
        if not model_path or not os.path.exists(model_path):
            logger.warning("MediaPipe model file missing, falling back to Haar Cascade")
            return None
            
        base_options = python.BaseOptions(model_asset_path=model_path)
        options = vision.FaceDetectorOptions(base_options=base_options)
        _mp_detector = vision.FaceDetector.create_from_options(options)
        logger.info("MediaPipe FaceDetector initialized successfully.")
        return _mp_detector
    except Exception as exc:
        logger.error("Failed to initialize MediaPipe FaceDetector: %s", exc)
        return None

def _get_haar_cascade():
    global _haar_cascade
    if _haar_cascade is not None:
        return _haar_cascade
    try:
        _haar_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        logger.info("Haar Cascade Face Detector initialized.")
        return _haar_cascade
    except Exception as exc:
        logger.error("Failed to initialize Haar Cascade Face Detector: %s", exc)
        return None

def detect_all_faces(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """
    Detect all faces and return their bounding boxes as a list of (x, y, w, h) in pixels.
    Uses MediaPipe Tasks FaceDetector, with a fallback to Haar Cascade.
    """
    img_h, img_w = img.shape[:2]
    
    # Try MediaPipe first
    mp_detector = _get_mp_detector()
    if mp_detector is not None:
        try:
            import mediapipe as mp
            rgb_image = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_image)
            detection_result = mp_detector.detect(mp_img)
            
            if detection_result and detection_result.detections:
                results = []
                for d in detection_result.detections:
                    bbox = d.bounding_box
                    x = max(0, int(bbox.origin_x))
                    y = max(0, int(bbox.origin_y))
                    w = min(img_w - x, int(bbox.width))
                    h = min(img_h - y, int(bbox.height))
                    results.append((x, y, w, h))
                if results:
                    return results
        except Exception as exc:
            logger.error("MediaPipe all faces inference error, trying Haar Cascade fallback: %s", exc)
            
    # Fallback to Haar Cascade
    cascade = _get_haar_cascade()
    if cascade is not None:
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(30, 30),
            )
            return [tuple(f) for f in faces]
        except Exception as exc:
            logger.error("Haar Cascade all faces inference error: %s", exc)
            
    return []


def detect_largest_face(img: np.ndarray) -> tuple[int, int, int, int] | None:
    """
    Detect the largest face and return its bounding box (x, y, w, h) in pixels.
    """
    faces = detect_all_faces(img)
    if not faces:
        return None
    # Pick the largest by area (w * h)
    largest = max(faces, key=lambda f: f[2] * f[3])
    return largest


def detect_face_with_keypoints(img: np.ndarray) -> dict | None:
    """
    Detect the largest face and return a dict with face bounding box and normalized keypoints.
    """
    img_h, img_w = img.shape[:2]
    mp_detector = _get_mp_detector()
    if mp_detector is not None:
        try:
            import mediapipe as mp
            rgb_image = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_image)
            detection_result = mp_detector.detect(mp_img)
            
            if detection_result and detection_result.detections:
                largest_det = None
                largest_area = 0
                for d in detection_result.detections:
                    bbox = d.bounding_box
                    area = bbox.width * bbox.height
                    if area > largest_area:
                        largest_area = area
                        largest_det = d
                        
                if largest_det:
                    bbox = largest_det.bounding_box
                    x = max(0, int(bbox.origin_x))
                    y = max(0, int(bbox.origin_y))
                    w = min(img_w - x, int(bbox.width))
                    h = min(img_h - y, int(bbox.height))
                    
                    keypoints = []
                    for k in largest_det.keypoints:
                        keypoints.append({
                            "x": k.x,
                            "y": k.y
                        })
                    return {
                        "box": (x, y, w, h),
                        "keypoints": keypoints
                    }
        except Exception as exc:
            logger.error("MediaPipe keypoints detection error: %s", exc)
            
    # Fallback to Haar Cascade (no keypoints)
    cascade = _get_haar_cascade()
    if cascade is not None:
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(faces) > 0:
                areas = [fw * fh for (_, _, fw, fh) in faces]
                idx = int(np.argmax(areas))
                fx, fy, fw, fh = faces[idx]
                return {
                    "box": (fx, fy, fw, fh),
                    "keypoints": []
                }
        except Exception as exc:
            logger.error("Cascade face detection error: %s", exc)
            
    return None


