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

    def test_sparse_scene_uses_detector_dense_uses_crowd_model(self):
        c = cam(role="density")
        c.crowd_total = 1.5            # crowd model reads one person as 1.5
        c.yolo_n, c.yolo_at = 1, app.time.monotonic()
        c.choose_people()
        self.assertEqual(c.people, 1)
        c.yolo_n, c.crowd_total = app.SPARSE_MAX + 5, 140.4
        c.choose_people()
        self.assertEqual(c.people, 140)
        c.yolo_n, c.crowd_total = 3, 150.0   # far-off crowd the detector mostly misses
        c.choose_people()
        self.assertEqual(c.people, 150)
        c.yolo_n, c.crowd_total, c.yolo_at = 1, 1.5, 0.0   # detector count too old to trust: the fresh crowd total is shown
        c.choose_people()
        self.assertEqual(c.people, 2)

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

    def test_login_flow(self):
        self.assertTrue(app.app.secret_key)  # set at import: a login POST as the very first request must not crash
        client = app.app.test_client()
        old, app.AUTH = app.AUTH, ("admin", "admin123")
        try:
            self.assertEqual(client.get("/api/cameras").status_code, 401)           # API: 401 JSON
            self.assertEqual(client.get("/").headers["Location"], "/login")         # page: redirect to login
            self.assertEqual(client.get("/login").status_code, 200)
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/static/monitor/img/cm.png").status_code, 302)  # photos need a login
            real_sleep, app.time.sleep = app.time.sleep, lambda s: None  # skip the 1 s delay on a bad password
            bad = client.post("/login", json={"username": "admin", "password": "nope"})
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(client.get("/nope").status_code, 302)                  # still logged out
            good = client.post("/login", json={"username": "admin", "password": "admin123", "redirect": "//evil.com"})
            self.assertEqual((good.status_code, good.get_json()["redirect"]), (200, "/"))  # open redirect refused
            self.assertEqual(client.get("/nope").status_code, 404)                  # past the login now
            self.assertEqual(client.get("/logout").status_code, 302)
            self.assertEqual(client.get("/api/cameras").status_code, 401)
        finally:
            app.AUTH = old
            app.time.sleep = real_sleep

    def test_connection_validation_and_url(self):
        conn = app.validated_connection({"host": "192.168.1.10", "user": "admin", "password": "p@ss/#", "path": "/ch1"})
        self.assertEqual(app.connection_url(conn), "rtsp://admin:p%40ss%2F%23@192.168.1.10:554/ch1")
        self.assertIsNone(app.validated_connection({"host": "  "}))  # blank host = use MediaMTX again
        for bad in ({"host": "a b"}, {"host": "10.0.0.1", "port": 70000}, {"host": "10.0.0.1", "path": "ch1"},
                    {"host": "10.0.0.1", "path": "/a b"}, {"host": "10.0.0.1", "user": "x\ny"}, {"host": "file:///etc"}):
            with self.assertRaises(ValueError, msg=bad):
                app.validated_connection(bad)
        kept = app.validated_connection({"host": "10.0.0.1"}, saved_password="saved")  # no password key: keep
        self.assertEqual(kept["password"], "saved")

    def test_sources_api_hides_password_and_keeps_it(self):
        c = cam(id="c1")
        engine = app.Engine.__new__(app.Engine)
        engine.lock, engine.cameras, engine.mediamtx, engine.thresholds = app.threading.Lock(), {"c1": c}, "rtsp://m", self.thresholds
        engine.save = lambda: None
        old, app.engine, app.AUTH = app.engine, engine, ("admin", "admin123")
        client = app.app.test_client()
        client.post("/login", json={"username": "admin", "password": "admin123"})
        try:
            r = client.post("/api/sources", json={"cameras": [{"id": "c1", "host": "10.0.0.7", "user": "u", "password": "secret", "path": "/s"}]})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("secret", r.get_data(as_text=True))
            self.assertTrue(client.get("/api/sources").get_json()["cameras"][0]["has_password"])
            client.post("/api/sources", json={"cameras": [{"id": "c1", "host": "10.0.0.8", "user": "u", "path": "/s"}]})
            self.assertEqual(c.conn["password"], "secret")                      # omitted password is kept
            self.assertEqual(c.url(), "rtsp://u:secret@10.0.0.8:554/s")
            self.assertEqual(client.post("/api/sources", json={"cameras": [{"id": "zz", "host": "1.2.3.4"}]}).status_code, 400)
            client.post("/api/sources", json={"cameras": [{"id": "c1", "host": ""}]})
            self.assertEqual(c.url(), "rtsp://x/c1")                            # cleared: back to the default source
        finally:
            app.engine, app.AUTH = old, ("admin", "admin123")

    def test_stats_never_expose_source(self):
        self.assertNotIn("source", cam().stats(self.thresholds))


if __name__ == "__main__":
    unittest.main()
