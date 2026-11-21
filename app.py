"""Multi-camera people analytics.

Cameras have a role: "inout" (tracked line crossings), "density" (people in view and
people per m2), "both" (tracked crossings plus density) or "off", and a location
(inside or outside the temple/ghat) used to split the people totals. One capture thread per camera keeps only the latest frame; a single
inference thread shares one model across all cameras and serves whichever is most overdue.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import cv2
from flask import Flask, Response, abort, jsonify, redirect, request, session
from people_analytics import LineCounter, estimate_camera_geometry, estimate_height_from_person

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
# Must be set before torch loads: otherwise its pool spins 7 busy-wait threads that starve the video decoders.
os.environ.setdefault("OMP_NUM_THREADS", os.getenv("INOUT_THREADS", "3"))
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
SHOW_BOXES = os.getenv("SHOW_BOXES", "0") == "1"   # boxes and track ids on the video; off by default (cleaner, cheaper)
VIEW_FPS = float(os.getenv("VIEW_FPS", "12"))        # video smoothness while a camera is open in the viewer
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


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validated_connection(c, saved_password=""):
    """Clean {host, port, user, password, path} from the Connect cameras screen, or None when host is blank.

    Only rtsp:// URLs are ever built from these fields, so a camera entry cannot read local files or
    other URL schemes. The caller is logged in; the host is still checked so typos fail early.
    """
    host = str(c.get("host") or "").strip()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOST_RE.match(host):
            raise ValueError("Host must be an IP address or a host name")
    try:
        port = int(c.get("port") or 554)
    except (TypeError, ValueError):
        raise ValueError("Port must be a number") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port must be 1-65535")
    user = str(c.get("user") or "")
    password = saved_password if c.get("password") is None else str(c["password"])
    path = str(c.get("path") or "/").strip()
    if len(user) > 64 or len(password) > 128 or any(ord(ch) < 32 for ch in user + password):
        raise ValueError("User or password is too long or has control characters")
    if not path.startswith("/") or len(path) > 300 or re.search(r"[\s\x00-\x1f]", path):
        raise ValueError("Stream path must start with / and contain no spaces")
    return {"host": host, "port": port, "user": user, "password": password, "path": path}


def connection_url(conn):
    host = f"[{conn['host']}]" if ":" in conn["host"] else conn["host"]
    auth = f"{quote(conn['user'], safe='')}:{quote(conn['password'], safe='')}@" if conn["user"] else ""
    return f"rtsp://{auth}{host}:{conn['port']}{conn['path']}"


def probe(url):
    """Try to open a stream and read one frame. Returns (ok, message); the URL is never echoed."""
    out = {}

    def run():
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 6000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])  # long keyframe gaps need a few seconds
        try:
            if not cap.isOpened():
                out["r"] = (False, "Could not connect. Check the IP, port, user, password and stream path.")
                return
            ok, frame = cap.read()
            out["r"] = (True, f"Connected, {frame.shape[1]}x{frame.shape[0]}") if ok else (False, "Connected but no video arrived.")
        finally:
            cap.release()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(18)
    return out.get("r", (False, "Timed out. Is the IP reachable from this server?"))


class Camera:
    def __init__(self, cfg, source, index):
        self.cfg, self.source, self.index = cfg, source, index
        self.conn, self.url_version = None, 0   # conn = IP/credentials set from the website; wins over source
        self.lock = threading.RLock()
        self.counter = LineCounter(cfg["line"], cfg["in_side"])
        self.tracker = None
        self.frame = None
        self.frame_seq = self.infer_seq = 0
        self.dets, self.people, self.visible = [], 0, 0   # people = crowd count (tracked count for inout)
        self.status = "starting"
        self.due = self.crowd_due = 0.0
        self.times = deque(maxlen=20)
        self.crowd_seq, self.counted_at, self.watch_until = 0, None, 0.0   # watch_until: someone is viewing this camera

    # ---- capture -------------------------------------------------------------------
    def set_status(self, status):
        with self.lock:
            self.status = status

    def url(self):
        return connection_url(self.conn) if self.conn else self.source

    def capture(self):
        time.sleep(self.index * 0.2)  # stagger RTSP connects
        cap, last, file_fps, failures, seen, is_file = None, 0.0, 25.0, 0, -1, False

        def pause(seconds):  # sleep, but wake early when the connection settings change
            for _ in range(int(seconds * 4)):
                if self.url_version != seen:
                    return
                time.sleep(0.25)

        while True:
            role = self.cfg["role"]
            if self.url_version != seen:  # new IP/credentials saved: reconnect now
                seen = self.url_version
                if cap is not None:
                    cap.release()
                    cap = None
                is_file = not self.url().lower().startswith(STREAM_PREFIXES)
            if role == "off":
                if cap is not None:
                    cap.release()
                    cap = None
                self.set_status("disabled")
                pause(1)
                continue
            if cap is None:
                cap = cv2.VideoCapture(self.url(), cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.set_status("offline")
                    pause(5)
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
                pause(2)
                continue
            failures = 0
            if is_file:
                time.sleep(1 / file_fps)  # files read unthrottled otherwise
            now = time.monotonic()
            gap = 1 / VIEW_FPS if now < self.watch_until else min(0.5, 1 / FPS[role])
            if now - last >= gap * 0.6:  # decode only the frames we can use (more while someone watches); 0.6 because frame ticks (40 ms at 25 fps) would otherwise round the rate down
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
        started = time.monotonic()
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
            self.due = started + 1 / TRACK_FPS   # period from the start of this check, not its end: otherwise 5 fps becomes ~4

    def count_crowd(self, counter):
        """Crowd count from the DM-Count + CSRNet fusion model (density / both)."""
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        total = counter.count(frame)
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:
                return
            self.crowd_seq, self.counted_at = seq, now
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
        """JPEG of the latest frame with the count line and the count only (boxes with SHOW_BOXES=1)."""
        with self.lock:
            frame, cfg, dets = self.frame, self.cfg, list(self.dets)
            counts, people = self.counter.counts(), self.people
        if frame is None:
            return None
        frame = frame.copy()
        h, w = frame.shape[:2]
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
        for track_id, (x1, y1, x2, y2) in (dets if SHOW_BOXES else ()):
            cv2.rectangle(frame, (round(x1), round(y1)), (round(x2), round(y2)), GREEN, 2)
            cv2.circle(frame, (round((x1 + x2) / 2), round(y2)), 4, YELLOW, -1)
            cv2.putText(frame, f"#{track_id}", (round(x1), max(20, round(y1) - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
        label = {"inout": f"IN {counts['in']}  OUT {counts['out']}",
                 "both": f"IN {counts['in']}  OUT {counts['out']}  PEOPLE {people}",
                 "density": f"PEOPLE {people}"}.get(cfg["role"], "")
        cv2.putText(frame, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        if width and width < w:
            frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
        return jpg.tobytes() if ok else None

    def snapshot(self):
        with self.lock:
            frame = self.frame
        if frame is None:
            return None
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return jpg.tobytes() if ok else None


class CrowdProcess:
    """Parent-side handle of crowd_worker.py (frames in, counts out)."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "crowd_worker.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, cwd=str(ROOT))
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("crowd worker exited at start-up (are the DM-Count/CSRNet weights present?)")
        self.mode = json.loads(line).get("mode", "static")

    def poll(self):
        return self.proc.poll()

    def count(self, frame):
        h, w = frame.shape[:2]
        if w > 1024:  # the model works at 512 px; do not push full-size frames through the pipe
            frame = cv2.resize(frame, (1024, round(h * 1024 / w)), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]
        self.proc.stdin.write(f"{h} {w}\n".encode() + frame.tobytes())
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("crowd worker stopped")
        return json.loads(line)["total"]

    def close(self):
        try:
            self.proc.kill()
        except OSError:
            pass


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
            cam = Camera(cfg, raw.get("source") or f"{self.mediamtx}/{cfg['id']}", i)
            cam.conn = validated_connection(raw.get("connection") or {})
            self.cameras[cfg["id"]] = cam
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
        import numpy as np
        import torch
        model.predict(np.zeros((64, 64, 3), np.uint8), imgsz=INOUT_IMGSZ, verbose=False)  # ultralytics resets torch to 7 threads on its first predict
        torch.set_num_threads(int(os.getenv("INOUT_THREADS", "3")))  # YOLOv8n at 480 px: ~48 ms/frame on 1 thread, ~38 ms on 3; the crowd model has its own process
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
        """Gate crossings are missed below ~5 checks/s, so crowd counting waits while any gate lags."""
        return any(c.cfg["role"] in TRACKING and c.status == "live" and len(c.times) > 5
                   and c.rate() < YIELD_RATIO * TRACK_FPS for c in self.cameras.values())

    def crowd_loop(self):
        """Counts crowds in a separate low-priority process, so the video and gate tracking never lag."""
        worker = None
        while True:
            try:
                if worker is None or worker.poll() is not None:
                    self.crowd_model = "loading"
                    worker = CrowdProcess()
                    self.crowd_model = "DM-Count + CSRNet fusion" + (" (learned head)" if worker.mode == "learned" else "")
                now = time.monotonic()
                ready = [c for c in self.cameras.values()
                         if c.cfg["role"] in DENSITY and c.frame is not None and c.crowd_due <= now]
                if not ready or self.gates_behind():
                    time.sleep(0.5)
                    continue
                cam = min(ready, key=lambda c: c.crowd_due)
                try:
                    cam.count_crowd(worker)
                except Exception:
                    log.exception("Crowd count failed for %s", cam.cfg["id"])
                    cam.crowd_due = time.monotonic() + 30
                    worker.close()          # a failed count means a broken pipe: restart the worker
                    worker = None
            except Exception as exc:
                log.exception("Crowd model unavailable; retrying in 30 s")
                self.crowd_model = f"unavailable: {exc}"
                if worker is not None:
                    worker.close()
                worker = None
                time.sleep(30)

    def save(self):
        out = {"mediamtx": self.mediamtx, "thresholds": self.thresholds, "cameras": []}
        for cam in self.cameras.values():
            entry = dict(cam.cfg)
            if not cam.source.startswith(self.mediamtx + "/"):
                entry["source"] = cam.source
            if cam.conn:
                entry["connection"] = cam.conn
            out["cameras"].append(entry)
        fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.chmod(tmp, 0o600)  # holds camera passwords
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


app.secret_key = session_key()   # set at import: Flask opens the session before any request handler runs


@app.before_request
def require_login():
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
            cam.counted_at, cam.crowd_due = None, 0.0
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


def source_row(cam):
    """What the browser may see of a camera's connection: never the password."""
    c = cam.conn or {}
    return {"id": cam.cfg["id"], "name": cam.cfg["name"], "host": c.get("host", ""), "port": c.get("port", 554),
            "user": c.get("user", ""), "path": c.get("path", ""), "has_password": bool(c.get("password"))}


@app.get("/api/sources")
def list_sources():
    return jsonify(cameras=[source_row(c) for c in engine.cameras.values()])


@app.post("/api/sources")
def save_sources():
    """Save IP / credentials for several cameras at once. A row without "password" keeps the saved one."""
    rows = (request.get_json(silent=True) or {}).get("cameras")
    if not isinstance(rows, list) or len(rows) > 200:
        return jsonify(error="Send a list of cameras"), 400
    with engine.lock:
        updates = {}
        for row in rows:
            cam = engine.cameras.get(row.get("id")) if isinstance(row, dict) else None
            if cam is None:
                return jsonify(error=f"Unknown camera {row.get('id') if isinstance(row, dict) else row!r}"), 400
            try:
                updates[cam.cfg["id"]] = validated_connection(row, (cam.conn or {}).get("password", ""))
            except ValueError as exc:
                return jsonify(error=f"{cam.cfg['name']}: {exc}"), 400
        for cam_id, conn in updates.items():
            cam = engine.cameras[cam_id]
            cam.conn = conn
            cam.url_version += 1
            with cam.lock:
                cam.tracker, cam.dets, cam.frame, cam.status = None, [], None, "starting"
        engine.save()
    return jsonify(saved=len(updates), cameras=[source_row(c) for c in engine.cameras.values()])


@app.post("/api/sources/test")
def test_source():
    data = request.get_json(silent=True) or {}
    cam = cam_or_404(data.get("id"))
    try:
        conn = validated_connection(data, (cam.conn or {}).get("password", ""))
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc))
    if conn is None:
        return jsonify(ok=False, message="Enter the camera or NVR IP address first.")
    ok, message = probe(connection_url(conn))
    return jsonify(ok=ok, message=message)


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
            cam.watch_until = time.monotonic() + 3        # keeps the camera decoding at VIEW_FPS while this stream is open
            if cam.frame_seq == last:
                time.sleep(0.02)
                continue
            last = cam.frame_seq
            jpg = cam.render()
            if jpg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            time.sleep(1 / VIEW_FPS)

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
