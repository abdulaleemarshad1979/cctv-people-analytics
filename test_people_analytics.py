import math
import unittest

from people_analytics import LineCounter, estimate_camera_geometry, estimate_height_from_person


class AnalyticsTests(unittest.TestCase):
    def test_two_people_cross_separately_once(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        counter.update([(1, (10, 10, 30, 40)), (2, (40, 10, 60, 42))], 100, 100, 1)
        counter.update([(1, (10, 40, 30, 70)), (2, (40, 40, 60, 72))], 100, 100, 2)
        counter.update([(1, (10, 42, 30, 75)), (2, (40, 42, 60, 77))], 100, 100, 3)
        self.assertEqual(counter.counts()["in"], 2)
        self.assertEqual(counter.counts()["out"], 0)

    def test_new_track_on_far_side_does_not_count(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        counter.update([(5, (10, 40, 30, 75))], 100, 100, 1)
        self.assertEqual(counter.counts()["in"], 0)

    def test_person_height_calibration(self):
        def image_y(ratio):
            return (1 + math.tan(math.atan(ratio) - math.radians(30)) /
                    math.tan(math.radians(25))) / 2
        head = image_y((3 - 1.7) / 5)
        foot = image_y(3 / 5)
        self.assertAlmostEqual(estimate_height_from_person(head, foot, 50, 30, 1.7), 3)
        geometry = estimate_camera_geometry({"camera_height_m": 3,
            "vertical_fov_deg": 50, "horizontal_fov_deg": 70, "tilt_deg": 30})
        self.assertGreater(geometry["far_m"], geometry["near_m"])
        self.assertGreater(geometry["far_width_m"], geometry["near_width_m"])


if __name__ == "__main__":
    unittest.main()
