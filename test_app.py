import unittest

import app


def cam(**over):
    cfg = {**app.DEFAULTS, "id": "c1", "name": "C1", **over}
    return app.Camera(app.validated(cfg), "rtsp://x/c1", 0)


class AppTests(unittest.TestCase):
    thresholds = {"warning": 2.0, "critical": 4.0}

    def test_worker_defaults_leave_room_for_video_and_cap_model_copies(self):
        self.assertEqual(app.default_worker_counts(8, 7), (3, 2))
        self.assertEqual(app.default_worker_counts(32, 7), (6, 2))
        self.assertEqual(app.default_worker_counts(32, 35), (6, 4))

    def test_playing_the_h264_copy_does_not_force_full_frame_decoding(self):
        c = cam()   # hub_base None: no encoder thread starts
        c.ensure_view()
        c.ensure_view("_viewhd")
        self.assertEqual(c.watch_until, 0.0)   # watch_until made the app decode every 5 MP frame and starved line counting
        self.assertGreater(c.view_until["_view"], 0.0)
        self.assertGreater(c.view_until["_viewhd"], 0.0)   # the big copy is requested on its own, by the opened viewer

    def test_gate_worker_gets_a_shrunk_frame_and_boxes_come_back_in_frame_pixels(self):
        import io, json
        import numpy as np
        sent = io.BytesIO()
        proc = type("P", (), {"stdin": sent, "stdout": io.BytesIO(b'{"boxes": [[10, 20, 30, 40]]}\n')})()
        worker = app.GateProcess.__new__(app.GateProcess)
        worker.proc = proc
        boxes = worker.boxes(np.zeros((1080, 1920, 3), np.uint8), imgsz=960)
        header = json.loads(sent.getvalue().split(b"\n", 1)[0])
        self.assertEqual((header["w"], header["h"]), (960, 540))   # shrunk to the detector size
        self.assertEqual(boxes, [(20.0, 40.0, 60.0, 80.0)])        # scaled back x2 to the full frame

    def _engine_with_history(self, directory):
        from history import History
        engine = app.Engine.__new__(app.Engine)
        engine.cameras = {"c1": cam()}
        engine.history = History(f"{directory}/history.db")
        return engine

    def test_counts_are_saved_once_and_survive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine_with_history(d)
            c = engine.cameras["c1"]
            c.counter.in_count, c.counter.out_count = 5, 2
            engine.flush_history()
            c.counter.in_count = 7
            engine.flush_history()
            engine.flush_history()                      # nothing new: must not add again
            self.assertEqual(engine.history.totals()["c1"], (7, 2))
            reborn = self._engine_with_history(d).cameras["c1"]   # after a restart the counter starts at 0 ...
            reborn.base_in, reborn.base_out = engine.history.totals()["c1"]
            reborn.counter.in_count = 1                 # ... and the shown total keeps going up from the saved one
            self.assertEqual(reborn.counts(), {"in": 8, "out": 2, "occupancy_change": 6})

    def test_report_csv_lists_hours_and_totals(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            engine = self._engine_with_history(d)
            engine.history.add_counts("c1", 4, 1)
            old, app.engine = app.engine, engine
            try:
                client = app.app.test_client()
                with client.session_transaction() as sess:
                    sess["user"] = app.AUTH[0]
                day = engine.history.hour_key()[:10]
                body = client.get(f"/api/report.csv?from={day}&to={day}").get_data(as_text=True)
                self.assertIn(",c1,C1,", body)
                self.assertIn(",4,1,3,", body)           # IN, OUT, net
                self.assertEqual(client.get("/api/report.csv?from=nope").status_code, 400)
            finally:
                app.engine = old

    def test_main_stream_path_of_a_sub_stream(self):
        self.assertEqual(app.main_path("/live1s2.sdp"), "/live1s1.sdp")
        self.assertIsNone(app.main_path("/live1s1.sdp"))   # already the main stream

    def test_published_for_reads_mediamtx_ready_time(self):
        ago = (app.datetime.now(app.timezone.utc) - app.timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        self.assertAlmostEqual(app.published_for({"readyTime": ago}), 7, delta=1.5)
        self.assertGreater(app.published_for({}), 1e6)   # no timestamp: do not hold the player back

    def test_only_active_cameras_appear_in_totals(self):
        engine = app.Engine.__new__(app.Engine)
        engine.thresholds, engine.rate_times, engine.crowd_model, engine.warnings = self.thresholds, [], "test", []
        engine.cameras = {str(i): cam(id=str(i)) for i in range(app.ACTIVE_CAMERA_COUNT + 1)}
        self.assertEqual(engine.summary()["totals"]["total"], app.ACTIVE_CAMERA_COUNT)

    def test_inactive_camera_relay_does_not_pull_its_nvr(self):
        engine = app.Engine.__new__(app.Engine)
        engine.cameras = {str(i): cam(id=str(i)) for i in range(app.ACTIVE_CAMERA_COUNT + 1)}
        for c in engine.cameras.values():
            c.hub_base = "rtsp://mtx:8554"
            c.conn = app.validated_connection({"host": "10.0.0.5", "path": "/live"})
        engine.relay_down, engine.hls_on = False, False
        seen = []
        old_call, old_ensure, old_hls = app.hub_call, app.hub_ensure, app.HLS_URL
        try:
            app.HLS_URL = ""
            app.hub_call = lambda method, path, body=None: (200, {"items": []})
            app.hub_ensure = lambda name, url, demand, existing: seen.append((name, demand))
            engine.sync_relay()
        finally:
            app.hub_call, app.hub_ensure, app.HLS_URL = old_call, old_ensure, old_hls
        self.assertEqual(seen[0], ("0", False))
        self.assertEqual(seen[-1], (str(app.ACTIVE_CAMERA_COUNT), True))

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
        c.size = (352, 288)
        stats = c.stats(self.thresholds)
        self.assertEqual((stats["in"], stats["out"], stats["level"]), (0, 0, "safe"))
        self.assertEqual(stats["resolution"], "352x288")

    def test_stopped_gate_worker_does_not_report_old_tracking_rate(self):
        c = cam(role="inout")
        now = app.time.monotonic()
        c.times.extend((now - 0.2, now - 0.1))
        self.assertGreater(c.stats(self.thresholds)["fps"], 0)
        c.times.clear()
        c.times.extend((now - 10.2, now - 10.1))
        stats = c.stats(self.thresholds)
        self.assertEqual(stats["fps"], 0)
        self.assertGreater(stats["tracked_ago"], 3)

    def test_overdue_head_counts_leave_capacity_for_gate_tracking(self):
        engine = app.Engine.__new__(app.Engine)
        gate1, gate2, head1, head2 = (cam(id="g1", role="inout"), cam(id="g2", role="inout"),
                                      cam(id="h1", role="density"), cam(id="h2", role="density"))
        engine.cameras = {c.cfg["id"]: c for c in (gate1, gate2, head1, head2)}
        engine.pick_lock = app.threading.Lock()
        engine.active_head_jobs = engine.gates_since_head = 0
        for c in engine.cameras.values():
            c.frame = object()
            c.frame_seq = 1
            c.due = c.detect_due = app.time.monotonic() - 10
        self.assertEqual(engine.pick_detector_job(), ("gate", gate1))
        engine.gates_since_head = 8
        kind, head = engine.pick_detector_job()
        self.assertEqual((kind, head), ("count", head1))
        self.assertEqual(engine.pick_detector_job(), ("gate", gate2))
        self.assertEqual(engine.active_head_jobs, 1)
        self.assertIsNone(engine.pick_detector_job())  # no second head check takes a gate worker

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
        engine.thresholds, engine.rate_times, engine.crowd_model, engine.warnings = self.thresholds, [], "test", []
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

    def test_hls_gateway_checks_login_and_known_camera(self):
        engine = app.Engine.__new__(app.Engine)
        engine.cameras = {"cam01": cam(id="cam01")}
        old_engine, app.engine = app.engine, engine
        client = app.app.test_client()
        try:
            path = {"X-Original-URI": "/hls/cam01/index.m3u8"}
            self.assertEqual(client.get("/authz", headers=path).status_code, 401)
            client.post("/login", json={"username": app.AUTH[0], "password": app.AUTH[1]})
            self.assertEqual(client.get("/authz", headers=path).status_code, 204)
            self.assertEqual(client.get("/authz", headers={"X-Original-URI": "/hls/cam02/index.m3u8"}).status_code, 403)
            self.assertEqual(client.get("/authz", headers={"X-Original-URI": "/hls/cam01/../../secret"}).status_code, 403)
        finally:
            app.engine = old_engine

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

    def test_playout_evens_out_a_burst_and_stays_live(self):
        burst = [(10.00, 1, "a"), (10.00, 2, "b"), (10.00, 3, "c")]    # three frames arrive together
        self.assertIsNone(app.next_playout(burst, 0, 10.05, 0.12))      # too fresh: wait
        shown, last = [], 0
        for now in (10.13, 10.17, 10.21):                               # one per tick afterwards, in order
            last = app.next_playout(burst, last, now, 0.12)[1]
            shown.append(last)
        self.assertEqual(shown, [1, 2, 3])
        many = [(10.0, i, i) for i in range(1, 8)]                      # far behind: jump to the newest
        self.assertEqual(app.next_playout(many, 0, 11.0, 0.12)[1], 7)

    def test_head_count_is_calibrated_and_median_smoothed(self):
        c = cam(count_scale=2.0, count_bias=1.0)
        c.crowd_hist = app.deque(maxlen=3)
        class Fake:
            values = iter([10.0, 11.0, 90.0])   # the last one is a glitch
            def count(self, frame):
                return next(self.values)
        c.frame = object()
        for _ in range(3):
            c.count_crowd(Fake())
        self.assertEqual(c.crowd_raw, 90.0)       # raw output stays visible
        self.assertEqual(c.crowd_total, 23.0)     # median of 21, 23, 181
        with self.assertRaises(ValueError):
            app.validated({**app.DEFAULTS, "id": "c1", "name": "C1", "count_scale": 0})

    def test_hub_paths_and_fallback(self):
        self.assertEqual(app.main_path("/cam/realmonitor?channel=2&subtype=1"), "/cam/realmonitor?channel=2&subtype=0")
        self.assertEqual(app.main_path("/Streaming/Channels/302"), "/Streaming/Channels/301")
        self.assertIsNone(app.main_path("/live"))
        c = cam(id="cam07")
        c.hub_base, c.hub_native = "rtsp://mtx:8554", False
        c.conn = app.validated_connection({"host": "10.0.0.5", "user": "u", "password": "p", "path": "/cam/realmonitor?channel=7&subtype=1"})
        wanted = c.hub_sources()
        self.assertEqual(sorted(wanted), ["cam07", "cam07_hd"])
        self.assertTrue(wanted["cam07"][0].endswith("subtype=1") and wanted["cam07_hd"][0].endswith("subtype=0"))
        self.assertEqual((wanted["cam07"][1], wanted["cam07_hd"][1]), (False, True))   # analysis always on; main stream only when watched
        self.assertTrue(c.url().startswith("rtsp://u:p@10.0.0.5"))      # MediaMTX down or not yet synced: read the camera directly
        c.hub = True
        self.assertEqual(c.url(), "rtsp://mtx:8554/cam07")
        old, app.WHEP_URL = app.WHEP_URL, "http://mtx:8889"
        try:
            c.registered = {"cam07", "cam07_hd"}
            self.assertEqual((c.play_path(), c.play_path(hd=True)), ("cam07", "cam07_hd"))
            c.hub = False
            c.hub_native = True                      # the default MediaMTX path name does not count while a typed-in connection is not registered
            self.assertIsNone(c.play_path())
        finally:
            app.WHEP_URL = old

    def test_webrtc_proxy_only_forwards_known_cameras(self):
        c = cam(id="cam01")
        engine = app.Engine.__new__(app.Engine)
        engine.cameras = {"cam01": c}
        old = app.engine, app.WHEP_URL, app.AUTH
        app.engine, app.WHEP_URL, app.AUTH = engine, "http://127.0.0.1:9", ("admin", "admin123")
        client = app.app.test_client()
        client.post("/login", json={"username": "admin", "password": "admin123"})
        try:
            for bad in ("zz/whep", "cam01/../x/whep", "cam01/other", "cam01/whep/not-a-session"):
                self.assertEqual(client.post(f"/webrtc/{bad}", data="v=0").status_code, 404, bad)
            self.assertEqual(client.post("/webrtc/cam01/whep", data="v=0").status_code, 502)   # MediaMTX unreachable: the page falls back to MJPEG
        finally:
            app.engine, app.WHEP_URL, app.AUTH = old

    def test_stats_never_expose_source(self):
        self.assertNotIn("source", cam().stats(self.thresholds))


if __name__ == "__main__":
    unittest.main()
