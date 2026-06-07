import unittest
import numpy as np
import cv2
from app.utils.overlay_utils import generate_condition_overlay, generate_redness_overlay
from app.services.overlay_storage import _slugify

class TestOverlayUtils(unittest.TestCase):
    def setUp(self):
        # Create a simple 100x100 white image
        img = np.ones((100, 100, 3), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".jpg", img)
        self.image_bytes = buf.tobytes()

    def test_slugify(self):
        self.assertEqual(_slugify("Acne / Pimples"), "acne_pimples")
        self.assertEqual(_slugify("Redness / Irritation"), "redness_irritation")
        self.assertEqual(_slugify("  Fine Lines / Wrinkles  "), "fine_lines_wrinkles")

    def test_generate_condition_overlay_no_regions(self):
        # With no regions, should return the original image bytes
        res = generate_condition_overlay(self.image_bytes, "Acne / Pimples", [])
        self.assertEqual(res, self.image_bytes)

    def test_generate_condition_overlay_valid(self):
        regions = [{"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.3}]
        res = generate_condition_overlay(self.image_bytes, "Acne / Pimples", regions)
        self.assertNotEqual(res, self.image_bytes)
        
        # Verify it decodes back to an image
        arr = np.frombuffer(res, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (100, 100, 3))

    def test_generate_redness_overlay(self):
        regions = [{"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}]
        res = generate_redness_overlay(self.image_bytes, regions, redness_score=45)
        self.assertNotEqual(res, self.image_bytes)
        
        arr = np.frombuffer(res, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (100, 100, 3))

if __name__ == "__main__":
    unittest.main()
