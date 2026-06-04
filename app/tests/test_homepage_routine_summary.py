import unittest
from unittest.mock import patch

from app.routers.homepage import _fetch_routine_summary


class FakeCursor(list):
    def sort(self, *_args, **_kwargs):
        return self


class HomepageRoutineSummaryTests(unittest.TestCase):
    def test_fetch_routine_summary_includes_product_details_and_completion(self):
        rows = [
            {
                "id": "step-1",
                "time": "morning",
                "product_category": "shampoo",
                "product_name": "Gentle Shampoo",
                "product_url": "https://example.test/shampoo",
                "is_completed": True,
            }
        ]

        fake_cursor = FakeCursor(rows)

        with patch("app.routers.homepage.saved_routines_collection.find", return_value=fake_cursor), \
             patch("app.routers.homepage._reset_stale_weekly_steps", side_effect=lambda rows: rows):
            result = _fetch_routine_summary("user-123")

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(
            result["data"],
            [
                {
                    "time": "morning",
                    "product_category": "shampoo",
                    "product_name": "Gentle Shampoo",
                    "product_url": "https://example.test/shampoo",
                    "is_completed": True,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
