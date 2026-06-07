# app/utils/model_downloader.py
import os
import logging
import urllib.request

logger = logging.getLogger(__name__)

MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
MODEL_PATH = "app/models/blaze_face_short_range.tflite"

def ensure_face_detector_model() -> str | None:
    """Ensure the MediaPipe face detector model is present. Downloads if missing."""
    if os.path.exists(MODEL_PATH):
        return MODEL_PATH
    
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    logger.info("MediaPipe face detector model not found. Downloading from %s...", MODEL_URL)
    
    try:
        # Download the file
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        logger.info("Successfully downloaded MediaPipe face detector model.")
        return MODEL_PATH
    except Exception as exc:
        logger.error("Failed to download model using urlretrieve: %s", exc)
        try:
            import httpx
            with httpx.Client(follow_redirects=True) as client:
                r = client.get(MODEL_URL)
                r.raise_for_status()
                with open(MODEL_PATH, "wb") as f:
                    f.write(r.content)
            logger.info("Successfully downloaded model via httpx.")
            return MODEL_PATH
        except Exception as exc2:
            logger.error("Alternative download failed: %s", exc2)
            if os.path.exists(MODEL_PATH):
                try:
                    os.remove(MODEL_PATH)
                except OSError:
                    pass
            return None
