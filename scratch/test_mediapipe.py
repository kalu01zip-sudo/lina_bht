import mediapipe as mp
import numpy as np

try:
    face_detection = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5)
    print("Mediapipe initialized successfully!")
    
    # Create a dummy image
    dummy_image = np.zeros((100, 100, 3), dtype=np.uint8)
    face_detection.process(dummy_image)
    print("Mediapipe process() executed successfully!")
    
except Exception as e:
    print(f"Mediapipe Error: {e}")
    import traceback
    traceback.print_exc()
