import unittest

import app


def cam(**over):
    cfg = {**app.DEFAULTS, "id": "c1", "name": "C1", **over}
    return app.Camera(app.validated(cfg), "rtsp://x/c1", 0)


class AppTests(unittest.TestCase):
    thresholds = {"warning": 2.0, "critical": 4.0}

    def test_validation_rejects_bad_input(self):
        for bad in ({"role": "gate"}, {"name": " "}, {"area_m2": 0}, {"tilt_deg": 120},
                    {"line": [[0.1, 0.1], [0.11, 0.1]]}):
            with self.assertRaises(ValueError, msg=bad):
                app.validated({**app.DEFAULTS, "id": "c1", "name": "C1", **bad})

    def test_density_levels_use_manual_area(self):
        c = cam(area_m2=10)
        for people, level in ((5, "safe"), (20, "warning"), (45, "critical")):
            c.people = people
            self.assertEqual(c.stats(self.thresholds)["level"], level)

    def test_density_unknown_without_area_or_height(self):
        stats = cam().stats(self.thresholds)
        self.assertIsNone(stats["density"])
        self.assertIsNone(stats["level"])

    def test_ground_area_from_calibration(self):
        area = app.ground_area({**app.DEFAULTS, "camera_height_m": 3})
        self.assertGreater(area, 0)  # horizon in view would give None

    def test_both_role_reports_crossings_and_density(self):
        c = cam(role="both", area_m2=10)
        c.people = 5
        stats = c.stats(self.thresholds)
        self.assertEqual((stats["in"], stats["out"], stats["level"]), (0, 0, "safe"))

    def test_location_must_be_inside_or_outside(self):
        with self.assertRaises(ValueError):
            cam(location="roof")
        self.assertEqual(cam(location="inside").stats(self.thresholds)["location"], "inside")

    def test_summary_splits_people_by_location(self):
        def fake(cam_id, location, role, people):
            c = cam(id=cam_id, location=location, role=role, area_m2=10)
            c.people = people
            return c
        engine = app.Engine.__new__(app.Engine)
        engine.thresholds, engine.rate_times, engine.crowd_model = self.thresholds, [], "test"
        engine.cameras = {c.cfg["id"]: c for c in (
            fake("a", "inside", "density", 30), fake("b", "inside", "both", 5),
            fake("c", "outside", "density", 12), fake("d", "outside", "inout", 7))}
        engine.cameras["b"].counter.in_count = 3
        totals = engine.summary()["totals"]
        self.assertEqual(totals["locations"]["inside"]["people"], 35)
        self.assertEqual(totals["locations"]["outside"]["people"], 12)  # in/out-only cameras add no crowd count
        self.assertEqual((totals["people"], totals["in"]), (47, 3))
        inside = totals["locations"]["inside"]  # 35 people over two 10 m2 cameras
        self.assertEqual((inside["area_m2"], inside["density"], inside["peak_density"]), (20.0, 1.75, 3.0))

    def test_login_required_when_configured(self):
        client = app.app.test_client()
        old = app.AUTH
        try:
            app.AUTH = ("admin", "s3cret")
            self.assertEqual(client.get("/api/cameras").status_code, 401)
            self.assertEqual(client.get("/healthz").status_code, 200)
            good = {"Authorization": "Basic YWRtaW46czNjcmV0"}  # admin:s3cret
            bad = {"Authorization": "Basic YWRtaW46d3Jvbmc="}   # admin:wrong
            self.assertEqual(client.get("/healthz", headers=good).status_code, 200)
            self.assertEqual(client.get("/nope", headers=bad).status_code, 401)
            self.assertEqual(client.get("/nope", headers=good).status_code, 404)  # past the login
        finally:
            app.AUTH = old

    def test_stats_never_expose_source(self):
        self.assertNotIn("source", cam().stats(self.thresholds))


if __name__ == "__main__":
    unittest.main()
