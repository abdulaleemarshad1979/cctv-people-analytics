import math
import unittest

from people_analytics import LineCounter, estimate_camera_geometry, estimate_height_from_person


class AnalyticsTests(unittest.TestCase):
    @staticmethod
    def walk(counter, path, x=50, at=0):
        """One person whose feet are at y (percent of a 100x100 frame) at each check."""
        for i, y in enumerate(path):
            counter.update([(x - 10, y - 30, x + 10, y)], 100, 100, at + i)

    def test_two_people_cross_separately_once(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        for at, (y1, y2) in enumerate(((40, 42), (52, 55), (60, 70), (62, 75))):
            counter.update([(10, y1 - 30, 30, y1), (60, y2 - 30, 80, y2)], 100, 100, at)
        self.assertEqual(counter.counts(), {"in": 2, "out": 0, "occupancy_change": 2})

    def test_in_then_out_and_direction_flip(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        self.walk(counter, (30, 42, 60))
        self.walk(counter, (60, 40), at=3)
        self.assertEqual((counter.counts()["in"], counter.counts()["out"]), (1, 1))
        flipped = LineCounter([[0.1, 0.5], [0.9, 0.5]], "negative")
        self.walk(flipped, (30, 42, 60))
        self.assertEqual((flipped.counts()["in"], flipped.counts()["out"]), (0, 1))

    def test_new_person_on_far_side_and_hovering_do_not_count(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        counter.update([(10, 45, 30, 75)], 100, 100, 1)
        self.walk(counter, (49.5, 50.5, 49.5, 50.5), x=80, at=2)   # inside the dead-band: no side change
        self.assertEqual(counter.counts()["in"] + counter.counts()["out"], 0)

    def test_crossing_beyond_the_line_end_does_not_count(self):
        counter = LineCounter([[0.2, 0.5], [0.8, 0.5]])
        self.walk(counter, (40, 60), x=5)
        self.assertEqual(counter.counts()["in"], 0)

    def test_person_gone_5_s_is_a_new_person(self):
        counter = LineCounter([[0.1, 0.5], [0.9, 0.5]])
        self.walk(counter, (40,))
        self.walk(counter, (60,), at=6)
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
