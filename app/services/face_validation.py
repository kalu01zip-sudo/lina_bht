import cv2
import numpy as np
from fastapi import HTTPException
# Load face detector
face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


def read_image(file_bytes: bytes):
    if not file_bytes:
        return None

    np_arr = np.frombuffer(file_bytes, np.uint8)

    if np_arr.size == 0:
        return None

    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    return image


def resize_image(image, max_width=800):
    h, w = image.shape[:2]

    if w > max_width:
        ratio = max_width / w
        new_width = max_width
        new_height = int(h * ratio)

        image = cv2.resize(image, (new_width, new_height))

    return image


def check_face(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )

    if len(faces) == 0:
        return "no_face"

    if len(faces) > 1:
        return "multiple_faces"

    x, y, face_w, face_h = faces[0]
    h, w, _ = image.shape

    # 🔥 size check
    if face_w < 120 or face_h < 120:
        return "face_too_small"

    if face_w > w * 0.9 or face_h > h * 0.9:
        return "invalid_face_size"

    # 🔥 shape check (VERY IMPORTANT)
    aspect_ratio = face_w / face_h
    if aspect_ratio < 0.6 or aspect_ratio > 1.6:
        return "invalid_face_shape"

    # 🔥 position check
    center_x = x + face_w / 2
    center_y = y + face_h / 2

    if center_x < w * 0.3 or center_x > w * 0.7:
        return "face_not_centered"

    if center_y < h * 0.3 or center_y > h * 0.7:
        return "face_not_centered"

    return "ok"
    
def check_face_size(image, detection):
    h, w, _ = image.shape

    bbox = detection.location_data.relative_bounding_box

    face_width = bbox.width * w
    face_height = bbox.height * h

    # Reject too small faces
    if face_width < 80 or face_height < 80:
        return "face_too_small"

    return "ok"

def check_face_position(image, detection):
    h, w, _ = image.shape
    bbox = detection.location_data.relative_bounding_box

    center_x = (bbox.xmin + bbox.width / 2) * w
    center_y = (bbox.ymin + bbox.height / 2) * h

    if center_x < w * 0.2 or center_x > w * 0.8:
        return "face_not_centered"

    if center_y < h * 0.2 or center_y > h * 0.8:
        return "face_not_centered"

    return "ok"


def check_brightness(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    brightness = np.mean(gray)

    if brightness < 50:
        return "too_dark"
    elif brightness > 200:
        return "too_bright"
    return "ok"


def check_blur(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    variance = cv2.Laplacian(gray, cv2.CV_64F).var()

    if variance < 50:
        return "blurry"
    return "ok"


def validate_image(file_bytes: bytes):
    image = read_image(file_bytes)

    if image is None:
        return "invalid_image"
    
    image = resize_image(image)
    if image is None:
        return "invalid_image"

    # checks
    face_status = check_face(image)
    if face_status != "ok":
        return face_status

    brightness_status = check_brightness(image)
    if brightness_status != "ok":
        return brightness_status

    blur_status = check_blur(image)
    if blur_status != "ok":
        return blur_status

    return "ok"