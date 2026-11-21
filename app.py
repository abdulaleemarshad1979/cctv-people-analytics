"""Multi-camera people analytics.

Cameras have a role: "inout" (tracked line crossings), "density" (people in view and
people per m2), "both" (tracked crossings plus density) or "off", and a location
(inside or outside the temple/ghat) used to split the people totals. One capture thread per camera keeps only the latest frame; a single
inference thread shares one model across all cameras and serves whichever is most overdue.
"""
import hashlib
import hmac
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, Response, abort, jsonify, redirect, request, session
from people_analytics import LineCounter, estimate_camera_geometry, estimate_height_from_person

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("analytics")
ROOT = Path(__file__).parent
app = Flask(__name__, static_folder=str(ROOT / "static"))
CONFIG_PATH = Path(os.getenv("ANALYTICS_CONFIG", str(ROOT / "cameras.json")))
MODEL_PATH = os.getenv("PEOPLE_MODEL", str(ROOT / "yolov8n.pt"))
INOUT_IMGSZ = int(os.getenv("INOUT_IMGSZ", "480"))
# Crossing tracker (YOLO + ByteTrack): drops crossings below ~5 checks/s (measured on a walker
# crossing 10 % of the frame per second). Crowd count (DM-Count + CSRNet fusion, ~1 s per frame on
# this CPU): one count per camera every CROWD_INTERVAL_S seconds.
TRACK_FPS = float(os.getenv("INOUT_FPS", "5"))
CROWD_INTERVAL = float(os.getenv("CROWD_INTERVAL_S", "60"))
YIELD_RATIO = float(os.getenv("GATE_YIELD_RATIO", "0.8"))  # crowd counting pauses while a gate runs below this share of its target
FPS = {"inout": TRACK_FPS, "both": TRACK_FPS, "density": 1 / CROWD_INTERVAL}
ROLES = ("inout", "density", "both", "off")
LOCATIONS = ("inside", "outside")
TRACKING = ("inout", "both")    # roles that run the tracker and count crossings
DENSITY = ("density", "both")   # roles that report people per m2
STREAM_PREFIXES = ("rtsp://", "rtmp://", "srt://", "http://", "https://")
DEFAULTS = {"zone": "", "location": "outside", "role": "density", "line": [[0.1, 0.5], [0.9, 0.5]], "in_side": "positive",
            "vertical_fov_deg": 50.0, "horizontal_fov_deg": 70.0, "tilt_deg": 30.0,
            "reference_height_m": 1.7, "camera_height_m": None, "area_m2": None}
EDITABLE = {"name", "zone", "location", "role", "line", "in_side", "vertical_fov_deg", "horizontal_fov_deg",
            "tilt_deg", "reference_height_m", "camera_height_m", "area_m2"}
GREEN, YELLOW = (55, 230, 110), (0, 230, 255)


def validated(cfg):
    """Return a cleaned camera config or raise ValueError."""
    cfg = dict(cfg)
    LineCounter.validate_line(cfg["line"])
    if cfg["in_side"] not in ("positive", "negative"):
        raise ValueError("Invalid IN direction")
    if cfg["role"] not in ROLES:
        raise ValueError("Role must be inout, density, both or off")
    if cfg["location"] not in LOCATIONS:
        raise ValueError("Location must be inside or outside")
    for key in ("name", "zone"):
        cfg[key] = str(cfg[key]).strip()[:60]
    if not cfg["name"]:
        raise ValueError("Name is required")
    for key, low, high in (("vertical_fov_deg", 1, 179), ("horizontal_fov_deg", 1, 179),
                           ("tilt_deg", -89, 89), ("reference_height_m", 0.1, 3),
                           ("camera_height_m", 0.2, 100), ("area_m2", 1, 1_000_000)):
        if cfg[key] is None and key in ("camera_height_m", "area_m2"):
            continue
        value = float(cfg[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        cfg[key] = value
    return cfg


def ground_area(cfg):
    """Visible level-ground area in m2 (trapezoid), or None when it cannot be bounded."""
    geo = estimate_camera_geometry(cfg)
    if not geo or geo["near_m"] is None or geo["far_m"] is None:
        return None
    return (geo["near_width_m"] + geo["far_width_m"]) / 2 * (geo["far_m"] - geo["near_m"])


class Camera:
    def __init__(self, cfg, source, index):
        self.cfg, self.source, self.index = cfg, source, index
        self.lock = threading.RLock()
        self.counter = LineCounter(cfg["line"], cfg["in_side"])
        self.tracker = None
        self.frame = None
        self.frame_seq = self.infer_seq = 0
        self.dets, self.people, self.visible = [], 0, 0   # people = crowd count (tracked count for inout)
        self.status = "starting"
        self.due = self.crowd_due = 0.0
        self.times = deque(maxlen=20)
        self.crowd_seq, self.dmap, self.counted_at = 0, None, None

    # ---- capture -------------------------------------------------------------------
    def set_status(self, status):
        with self.lock:
            self.status = status

    def capture(self):
        time.sleep(self.index * 0.2)  # stagger RTSP connects
        is_file = not str(self.source).lower().startswith(STREAM_PREFIXES)
        cap, last, file_fps, failures = None, 0.0, 25.0, 0
        while True:
            role = self.cfg["role"]
            if role == "off":
                if cap is not None:
                    cap.release()
                    cap = None
                self.set_status("disabled")
                time.sleep(1)
                continue
            if cap is None:
                cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.set_status("offline")
                    time.sleep(5)
                    continue
                file_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                with self.lock:
                    self.tracker = None
                    self.counter.forget_tracks()
            if not cap.grab():
                failures += 1
                if is_file and failures < 3:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop test files
                    continue
                cap.release()
                cap, failures = None, 0
                self.set_status("offline")
                time.sleep(2)
                continue
            failures = 0
            if is_file:
                time.sleep(1 / file_fps)  # files read unthrottled otherwise
            now = time.monotonic()
            if now - last >= min(0.5, 1 / FPS[role]):  # decode only the frames we can use
                ok, frame = cap.retrieve()
                if ok:
                    last = now
                    with self.lock:
                        self.frame, self.frame_seq, self.status = frame, self.frame_seq + 1, "live"

    # ---- inference -----------------------------------------------------------------
    def infer(self, model, tracker_args):
        """Track people for line crossings (inout / both)."""
        from ultralytics.trackers.byte_tracker import BYTETracker
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        h, w = frame.shape[:2]
        boxes = model.predict(frame, classes=[0], conf=0.12, iou=0.8, imgsz=INOUT_IMGSZ, verbose=False)[0].boxes.cpu().numpy()  # conf/iou as in Count peoplr
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:  # role changed mid-inference; drop the result
                return
            self.infer_seq = seq
            if self.tracker is None:
                args = type(tracker_args)(**{**vars(tracker_args), "track_buffer": max(2, int(TRACK_FPS * 3))})
                self.tracker = BYTETracker(args)
            tracks = self.tracker.update(boxes, frame)
            self.dets = [(int(t[4]), tuple(map(float, t[:4]))) for t in tracks]
            self.counter.update(self.dets, w, h, now)
            self.visible = len(self.dets)
            if role == "inout":
                self.people = self.visible
            self.times.append(now)
            self.due = now + 1 / TRACK_FPS

    def count_crowd(self, counter):
        """Crowd count from the DM-Count + CSRNet fusion model (density / both)."""
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        total, dmap = counter.count(frame)
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:
                return
            self.crowd_seq, self.dmap, self.counted_at = seq, dmap, now
            self.people = round(total)
            self.crowd_due = now + CROWD_INTERVAL

    # ---- views ---------------------------------------------------------------------
    def rate(self):
        with self.lock:
            t = list(self.times)
        return round((len(t) - 1) / (t[-1] - t[0]), 2) if len(t) > 1 and t[-1] > t[0] else 0.0

    def stats(self, thresholds):
        with self.lock:
            cfg, people, status = self.cfg, self.people, self.status
            counts = self.counter.counts()
        counted_at, visible = self.counted_at, self.visible
        out = {"id": cfg["id"], "name": cfg["name"], "zone": cfg["zone"], "location": cfg["location"], "role": cfg["role"],
               "status": status, "people": people, "visible": visible, "fps": self.rate(),
               "target_fps": TRACK_FPS if cfg["role"] in TRACKING else 0}
        if cfg["role"] in TRACKING:
            out.update(counts)
        if cfg["role"] in DENSITY:
            area = cfg["area_m2"] or ground_area(cfg)
            density = round(people / area, 2) if area else None
            level = None if density is None else (
                "critical" if density >= thresholds["critical"] else
                "warning" if density >= thresholds["warning"] else "safe")
            out.update(area_m2=round(area, 1) if area else None, density=density, level=level,
                       counted_ago=None if counted_at is None else round(time.monotonic() - counted_at))
        return out

    def render(self, width=None):
        """Annotated JPEG of the latest frame, or None when no frame has arrived."""
        with self.lock:
            frame, cfg, dets = self.frame, self.cfg, list(self.dets)
            counts, people, dmap = self.counter.counts(), self.people, self.dmap
        if frame is None:
            return None
        frame = frame.copy()
        h, w = frame.shape[:2]
        if cfg["role"] in DENSITY and dmap is not None:  # density heat map from the fusion model
            heat = cv2.resize(dmap, (w, h), interpolation=cv2.INTER_LINEAR)
            heat = np.clip(heat / max(float(heat.max()), 1e-6), 0, 1)
            colour = cv2.applyColorMap((heat * 255).astype(np.uint8), cv2.COLORMAP_JET)
            frame = np.where((heat > 0.12)[..., None], cv2.addWeighted(frame, 0.6, colour, 0.4, 0), frame)
        if cfg["role"] in TRACKING:
            a, b = cfg["line"]
            cv2.line(frame, (round(a[0] * w), round(a[1] * h)), (round(b[0] * w), round(b[1] * h)), YELLOW, 2)
            # Arrow along the normal that LineCounter treats as the "positive" side; flip for in_side "negative".
            nx, ny = (a[1] - b[1]) * w, (b[0] - a[0]) * h
            sign, norm = (1 if cfg["in_side"] == "positive" else -1), math.hypot(nx, ny) or 1
            mx, my = (a[0] + b[0]) / 2 * w, (a[1] + b[1]) / 2 * h
            tip = (round(mx + sign * nx / norm * 45), round(my + sign * ny / norm * 45))
            cv2.arrowedLine(frame, (round(mx), round(my)), tip, YELLOW, 3, tipLength=0.4)
            cv2.putText(frame, "IN", (tip[0] + 6, tip[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, YELLOW, 2)
        for track_id, (x1, y1, x2, y2) in dets:
            cv2.rectangle(frame, (round(x1), round(y1)), (round(x2), round(y2)), GREEN, 2)
            cv2.circle(frame, (round((x1 + x2) / 2), round(y2)), 4, YELLOW, -1)
            cv2.putText(frame, f"#{track_id}", (round(x1), max(20, round(y1) - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
        label = {"inout": f"IN {counts['in']}  OUT {counts['out']}  VISIBLE {people}",
                 "both": f"IN {counts['in']}  OUT {counts['out']}  PEOPLE {people}",
                 "density": f"PEOPLE {people}"}.get(cfg["role"], "")
        cv2.putText(frame, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        if width and width < w:
            frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
        return jpg.tobytes() if ok else None

    def snapshot(self):
        with self.lock:
            frame = self.frame
        if frame is None:
            return None
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return jpg.tobytes() if ok else None


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        data = json.loads(CONFIG_PATH.read_text())
        self.mediamtx = data.get("mediamtx", "rtsp://127.0.0.1:8554").rstrip("/")
        self.thresholds = {"warning": 2.0, "critical": 4.0, **data.get("thresholds", {})}
        if not 0 < self.thresholds["warning"] < self.thresholds["critical"]:
            raise ValueError("thresholds need 0 < warning < critical")
        self.cameras = {}
        for i, raw in enumerate(data["cameras"]):
            cfg = validated({**DEFAULTS, "name": raw["id"], **{k: v for k, v in raw.items() if k != "source"}})
            if cfg["id"] in self.cameras:
                raise ValueError(f"Duplicate camera id {cfg['id']}")
            self.cameras[cfg["id"]] = Camera(cfg, raw.get("source") or f"{self.mediamtx}/{cfg['id']}", i)
        self.rate_times = deque(maxlen=60)
        self.crowd_model = "loading"

    def start(self):
        for cam in self.cameras.values():
            threading.Thread(target=cam.capture, name=f"cap-{cam.cfg['id']}", daemon=True).start()
        threading.Thread(target=self.infer_loop, name="inference", daemon=True).start()
        threading.Thread(target=self.crowd_loop, name="crowd", daemon=True).start()

    def infer_loop(self):
        from ultralytics import YOLO
        from ultralytics.utils import IterableSimpleNamespace, YAML
        from ultralytics.utils.checks import check_yaml
        log.info("Loading people model %s", MODEL_PATH)
        model = YOLO(MODEL_PATH)  # only this thread ever calls the model
        tracker_args = IterableSimpleNamespace(**YAML.load(check_yaml("bytetrack.yaml")))
        while True:
            now = time.monotonic()
            ready = [c for c in self.cameras.values()
                     if c.cfg["role"] in TRACKING and c.frame is not None
                     and c.frame_seq != c.infer_seq and c.due <= now]
            if not ready:
                time.sleep(0.02)
                continue
            cam = min(ready, key=lambda c: c.due)
            try:
                cam.infer(model, tracker_args)
                self.rate_times.append(time.monotonic())
            except Exception:
                log.exception("Inference failed for %s", cam.cfg["id"])
                cam.due = time.monotonic() + 5

    def gates_behind(self):
        """Gate crossings are missed below ~5 checks/s, so crowd counting yields while any gate lags."""
        return any(c.cfg["role"] in TRACKING and c.status == "live" and len(c.times) > 5
                   and c.rate() < YIELD_RATIO * TRACK_FPS for c in self.cameras.values())

    def crowd_loop(self):
        """Second thread, so the ~1 s fusion model never stalls gate tracking."""
        try:
            from crowd_model import CrowdCounter
            counter = CrowdCounter()
        except Exception as exc:
            log.exception("Crowd model failed to load; density cameras will not count")
            self.crowd_model = f"unavailable: {exc}"
            return
        self.crowd_model = "DM-Count + CSRNet fusion" + (" (learned head)" if counter.mode == "learned" else "")
        while True:
            now = time.monotonic()
            ready = [c for c in self.cameras.values()
                     if c.cfg["role"] in DENSITY and c.frame is not None and c.crowd_due <= now]
            if not ready or self.gates_behind():
                time.sleep(0.5)
                continue
            cam = min(ready, key=lambda c: c.crowd_due)
            try:
                cam.count_crowd(counter)
            except Exception:
                log.exception("Crowd count failed for %s", cam.cfg["id"])
                cam.crowd_due = time.monotonic() + 30

    def save(self):
        out = {"mediamtx": self.mediamtx, "thresholds": self.thresholds, "cameras": []}
        for cam in self.cameras.values():
            entry = dict(cam.cfg)
            if not cam.source.startswith(self.mediamtx + "/"):
                entry["source"] = cam.source
            out["cameras"].append(entry)
        fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.chmod(tmp, CONFIG_PATH.stat().st_mode & 0o777)  # source URLs may hold credentials
        os.replace(tmp, CONFIG_PATH)

    def infer_rate(self):
        t = list(self.rate_times)
        return round((len(t) - 1) / (t[-1] - t[0]), 1) if len(t) > 1 and t[-1] > t[0] else 0.0

    def summary(self):
        cams = [c.stats(self.thresholds) for c in self.cameras.values()]
        locations = {}
        for name in LOCATIONS:
            here = [c for c in cams if c["location"] == name]
            gates = [c for c in here if c["role"] in TRACKING]
            crowd = [c for c in here if c["role"] in DENSITY]
            sized = [c for c in crowd if c["density"] is not None]
            area = sum(c["area_m2"] for c in sized)
            locations[name] = {"cameras": sum(c["role"] != "off" for c in here),
                               "people": sum(c["people"] for c in crowd),
                               "area_m2": round(area, 1) or None,
                               "density": round(sum(c["people"] for c in sized) / area, 2) if area else None,
                               "peak_density": max((c["density"] for c in sized), default=None),
                               "in": sum(c["in"] for c in gates), "out": sum(c["out"] for c in gates)}
        dens = [c for c in cams if c["role"] in DENSITY and c["density"] is not None]
        peak = max(dens, key=lambda c: c["density"], default=None)
        total_in = sum(v["in"] for v in locations.values())
        total_out = sum(v["out"] for v in locations.values())
        return {"cameras": cams, "thresholds": self.thresholds,
                "totals": {"people": sum(v["people"] for v in locations.values()), "locations": locations,
                           "in": total_in, "out": total_out,
                           "peak_density": peak["density"] if peak else None,
                           "peak_camera": peak["name"] if peak else None,
                           "online": sum(c["status"] == "live" for c in cams),
                           "total": len(cams), "infer_per_s": self.infer_rate(),
                           "crowd_model": self.crowd_model}}


engine = None


def cam_or_404(cam_id):
    cam = engine.cameras.get(cam_id)
    if cam is None:
        abort(404)
    return cam


def detail(cam):
    """Per-camera payload for the editor. Never includes the source URL (may hold credentials)."""
    with cam.lock:
        cfg = dict(cam.cfg)
    return {**cam.stats(engine.thresholds), "config": cfg, "geometry": estimate_camera_geometry(cfg)}


# Login: one account, a cookie session and the CCTV_MONITOR-style login page.
AUTH = (os.getenv("LOGIN_USER", "admin"), os.getenv("LOGIN_PASSWORD", "admin123"))
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/static/monitor/img/ap_police_logo.png"}  # logo is shown on the login page
app.config.update(SESSION_COOKIE_NAME="analytics_session", SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))


def session_key():
    """Stable across restarts (so users stay logged in) and changes with the password (logging everyone out)."""
    return os.getenv("SECRET_KEY") or hashlib.sha256(f"analytics|{AUTH[0]}|{AUTH[1]}".encode()).hexdigest()


def safe_redirect(target):
    """Only same-site paths: blocks open redirects such as //evil.com or https://evil.com."""
    target = str(target or "/")
    bad = not target.startswith("/") or target.startswith(("//", "/\\")) or "://" in target
    return "/" if bad else target


@app.before_request
def require_login():
    app.secret_key = session_key()
    if request.path in PUBLIC_PATHS or session.get("user") == AUTH[0]:
        return None
    if request.path.startswith("/api/"):
        return jsonify(detail="Login required"), 401
    return redirect("/login" if request.path == "/" else f"/login?redirect={quote(request.full_path.rstrip('?'))}")


@app.get("/login")
def login_page():
    if session.get("user") == AUTH[0]:
        return redirect(safe_redirect(request.args.get("redirect")))
    return app.send_static_file("login.html")


@app.post("/login")
def login_submit():
    data = request.get_json(silent=True) or {}
    ok = (hmac.compare_digest(str(data.get("username", "")).strip(), AUTH[0])
          & hmac.compare_digest(str(data.get("password", "")), AUTH[1]))
    if not ok:
        time.sleep(1)  # slows password guessing
        return jsonify(detail="Invalid username or password."), 401
    session.clear()
    session["user"] = AUTH[0]
    session.permanent = True
    return jsonify(status="success", redirect=safe_redirect(data.get("redirect")))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.get("/api/cameras")
def list_cameras():
    return jsonify(engine.summary())


@app.get("/api/cameras/<cam_id>")
def get_camera(cam_id):
    return jsonify(detail(cam_or_404(cam_id)))


@app.post("/api/cameras/<cam_id>/config")
def set_config(cam_id):
    cam = cam_or_404(cam_id)
    data = request.get_json(silent=True) or {}
    if not set(data) <= EDITABLE:
        return jsonify(error="Unknown configuration field"), 400
    with engine.lock, cam.lock:
        try:
            updated = validated({**cam.cfg, **data})
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        if (updated["line"], updated["in_side"]) != (cam.cfg["line"], cam.cfg["in_side"]):
            cam.counter.set_line(updated["line"], updated["in_side"])
        if updated["role"] != cam.cfg["role"]:
            cam.tracker, cam.dets, cam.people, cam.visible, cam.due = None, [], 0, 0, 0.0
            cam.dmap, cam.counted_at, cam.crowd_due = None, None, 0.0
            cam.times.clear()  # old-role rate history would trigger a false "too slow"
            cam.counter.forget_tracks()
        cam.cfg = updated
        engine.save()
    return jsonify(detail(cam))


@app.post("/api/cameras/<cam_id>/calibrate-height")
def calibrate_height(cam_id):
    cam = cam_or_404(cam_id)
    data = request.get_json(silent=True) or {}
    with engine.lock, cam.lock:
        try:
            height = estimate_height_from_person(
                float(data["head_y"]), float(data["foot_y"]), cam.cfg["vertical_fov_deg"],
                cam.cfg["tilt_deg"], cam.cfg["reference_height_m"])
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        cam.cfg["camera_height_m"] = round(height, 2)
        engine.save()
    return jsonify(detail(cam))


@app.post("/api/cameras/<cam_id>/reset-counts")
def reset_counts(cam_id):
    cam = cam_or_404(cam_id)
    with cam.lock:
        cam.counter.reset_counts()
    return jsonify(detail(cam))


def jpeg_response(jpg):
    return Response(jpg or b"", mimetype="image/jpeg", status=200 if jpg else 503,
                    headers={"Cache-Control": "no-store"})


@app.get("/api/cameras/<cam_id>/frame.jpg")
def frame(cam_id):
    width = min(max(request.args.get("w", type=int) or 0, 0), 1920) or None
    return jpeg_response(cam_or_404(cam_id).render(width))


@app.get("/api/cameras/<cam_id>/snapshot.jpg")
def snapshot(cam_id):
    return jpeg_response(cam_or_404(cam_id).snapshot())


@app.get("/api/cameras/<cam_id>/stream.mjpg")
def stream(cam_id):
    cam = cam_or_404(cam_id)

    def frames():
        last = -1
        while True:
            if cam.frame_seq == last:
                time.sleep(0.1)
                continue
            last = cam.frame_seq
            jpg = cam.render()
            if jpg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(0.2)

    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def create_engine():
    global engine
    engine = Engine()
    engine.start()
    return engine


if __name__ == "__main__":
    create_engine()
    app.run(host=os.getenv("WEB_HOST", "127.0.0.1"), port=int(os.getenv("WEB_PORT", "5000")),
            threaded=True, debug=False)
