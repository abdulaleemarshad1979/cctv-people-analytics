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
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import cv2
from flask import Flask, Response, abort, jsonify, redirect, request, session
from people_analytics import LineCounter, estimate_camera_geometry, estimate_height_from_person

# Low-latency RTSP read, as CCTV_MONITOR: no input buffering. (Its short probe options are left out: they can fail on cameras that send codec info late.)
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                      "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")
# Must be set before torch loads: otherwise its pool spins 7 busy-wait threads that starve the video decoders.
# Every detector / crowd worker runs on one torch thread (the most CPU-efficient size); throughput comes from more workers.
os.environ.setdefault("OMP_NUM_THREADS", "1")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("analytics")
ROOT = Path(__file__).parent
app = Flask(__name__, static_folder=str(ROOT / "static"))
CONFIG_PATH = Path(os.getenv("ANALYTICS_CONFIG", str(ROOT / "cameras.json")))
MODEL_PATH = os.getenv("PEOPLE_MODEL", str(ROOT / "yolov8n.pt"))
INOUT_IMGSZ = int(os.getenv("INOUT_IMGSZ", "640"))   # detector size: native resolution for a 640x360 sub-stream
INOUT_CONF = float(os.getenv("INOUT_CONFIDENCE", "0.35"))   # gate detector confidence, as active_pixel_html_camera_site
# Crossing counter (YOLO + nearest-point matching): drops crossings below ~5 checks/s (measured on a walker
# crossing 10 % of the frame per second). Crowd count (DM-Count + CSRNet fusion, ~2.9 core-seconds per frame):
# one count per density camera every CROWD_INTERVAL_S seconds.
# This server is dedicated to the cameras, so the cores are split between the two kinds of work by core count
# (override with INOUT_WORKERS / CROWD_WORKERS): reserve cores for camera decoding and
# web video. Each crowd worker holds its own copy of both models in RAM. Beyond six
# detector / four crowd workers, extra model copies can make a 32 GB server swap or
# restart while doing little for throughput.
CORES = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 4)
ACTIVE_CAMERA_COUNT = max(1, int(os.getenv("ACTIVE_CAMERA_COUNT", "7")))


def default_worker_counts(cores, active_cameras):
    # At this input size a crowd process typically handles about five cameras
    # per 30 s round. Do not load four model copies for seven live feeds.
    gates = min(6, active_cameras, max(1, round(cores * 0.35)))
    crowd = min(4, max(1, math.ceil(active_cameras / 5)), max(1, round(cores * 0.25)))
    return gates, crowd


DEFAULT_INOUT_WORKERS, DEFAULT_CROWD_WORKERS = default_worker_counts(CORES, ACTIVE_CAMERA_COUNT)
INOUT_WORKERS = int(os.getenv("INOUT_WORKERS") or DEFAULT_INOUT_WORKERS)
CROWD_WORKERS = int(os.getenv("CROWD_WORKERS") or DEFAULT_CROWD_WORKERS)
TRACK_FPS = float(os.getenv("INOUT_FPS", "10"))
CROWD_INTERVAL = float(os.getenv("CROWD_INTERVAL_S", "30"))
SHOW_BOXES = os.getenv("SHOW_BOXES", "0") == "1"   # boxes and track ids on the video; off by default (cleaner, cheaper)
# Live viewer JPEG width and quality. Measured on the site's night pictures: 1280 @ 85 = ~280 KB a frame (~55 Mbit/s at 25 fps),
# more than many remote links carry; 960 @ 70 = ~120 KB. Tiles (480 px) are ~36 KB at 70.
VIEW_WIDTH = int(os.getenv("VIEW_WIDTH", "960"))
VIEW_QUALITY = int(os.getenv("VIEW_QUALITY", "70"))
VIEW_FPS = float(os.getenv("VIEW_FPS", "25"))        # max viewer frame rate; match the camera (25) so no frames are dropped unevenly
VIEW_DELAY = float(os.getenv("VIEW_DELAY_MS", "0")) / 1000   # MJPEG fallback only: hold frames this long so a burst from the network is spread out (100-150 on a bursty link; adds that much delay; not measured to help)
# Same detector settings as CCTV_MONITOR (conf 0.30, iou 0.55, agnostic NMS). The crowd model counts every density camera
# every CROWD_INTERVAL; it reads a lone person as 1.5-4, so while the detector sees <= SPARSE_MAX people and the crowd model
# <= 2x that, the detector's count is shown. Otherwise the crowd model's, never below what the detector sees.
SPARSE_MAX = int(os.getenv("SPARSE_MAX", "10"))
# Video to the browser goes through MediaMTX over WebRTC (no re-encoding here; the browser decodes the camera's own H.264), as in CCTV_MONITOR.
# MEDIAMTX_API / MEDIAMTX_WEBRTC_URL are set by docker compose; blank = off and the viewer uses the MJPEG stream.
RELAY_API = os.getenv("MEDIAMTX_API", "").rstrip("/")
WHEP_URL = os.getenv("MEDIAMTX_WEBRTC_URL", "").rstrip("/")
# Live video as LL-HLS from MediaMTX, through this app's login: the camera's own H.264/H.265 passed through (no server CPU),
# buffered ~1 s by the browser so it plays evenly. Works over the web port alone (WebRTC needs UDP/8189 open).
HLS_URL = os.getenv("MEDIAMTX_HLS_URL", "").rstrip("/")
HLS_SETTINGS = {"hls": True, "hlsAddress": ":8888", "hlsVariant": "lowLatency", "hlsSegmentCount": 20,
                "hlsSegmentDuration": "1s", "hlsPartDuration": "200ms"}
# Density cameras nobody is watching are analysed once every few seconds, so ffmpeg decodes only their keyframes:
# ~40x less CPU than decoding every frame of a 5 MP H.265 stream (measured), left to the gate counters instead.
FFMPEG = shutil.which("ffmpeg")
HEALTH_S = float(os.getenv("HEALTH_CHECK_S", "30"))
DETECT_INTERVAL = float(os.getenv("DETECT_INTERVAL_S", "30"))   # detector count per density camera; every 2 s while someone watches it
COUNT_WINDOW = int(os.getenv("COUNT_TEMPORAL_WINDOW", "1"))   # median of the last N head counts (30 s apart) drops one-off glitches but delays a real surge; 1 = off, as CCTV_MONITOR on CPU
COUNT_CONF = float(os.getenv("YOLO_COUNT_CONFIDENCE", "0.40"))   # CCTV_MONITOR uses 0.30, but on a close-up camera a faint 0.28 box counted one person as 2
FPS = {"inout": TRACK_FPS, "both": TRACK_FPS, "density": 1 / CROWD_INTERVAL}
ROLES = ("inout", "density", "both", "off")
LOCATIONS = ("inside", "outside")
TRACKING = ("inout", "both")    # roles that run the tracker and count crossings
DENSITY = ("density", "both")   # roles that report people per m2
STREAM_PREFIXES = ("rtsp://", "rtmp://", "srt://", "http://", "https://")
DEFAULTS = {"zone": "", "location": "outside", "role": "density", "line": [[0.1, 0.5], [0.9, 0.5]], "in_side": "positive",
            "vertical_fov_deg": 50.0, "horizontal_fov_deg": 70.0, "tilt_deg": 30.0,
            "reference_height_m": 1.7, "camera_height_m": None, "area_m2": None,
            "count_scale": 1.0, "count_bias": 0.0}   # head-count calibration from hand counts: see scripts/calibrate_counts.py
EDITABLE = {"name", "zone", "location", "role", "line", "in_side", "vertical_fov_deg", "horizontal_fov_deg",
            "tilt_deg", "reference_height_m", "camera_height_m", "area_m2", "count_scale", "count_bias"}
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
                           ("camera_height_m", 0.2, 100), ("area_m2", 1, 1_000_000),
                           ("count_scale", 0.1, 10), ("count_bias", -500, 500)):
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


def main_path(path):
    """Main-stream path for a sub-stream path (Dahua/CP PLUS subtype=1 -> 0, Hikvision .../NN02 -> NN01), or None."""
    if "subtype=1" in path:
        return path.replace("subtype=1", "subtype=0")
    m = re.fullmatch(r"(/Streaming/Channels/\d+)02", path)
    return m.group(1) + "01" if m else None


def hub_call(method, path, body=None):
    """MediaMTX control API call: (status, json). Raises OSError when MediaMTX is unreachable."""
    req = urllib.request.Request(RELAY_API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def hub_ensure(name, url, on_demand, existing):
    """Make MediaMTX path `name` pull `url` (`existing`: its configured paths by name); changes nothing, so drops no viewer, when it already does."""
    want = {"source": url, "sourceOnDemand": on_demand, "rtspTransport": "tcp"}
    have = existing.get(name)
    if have and all(have.get(k) == v for k, v in want.items()):
        return
    status, _ = hub_call("POST", f"/v3/config/paths/{'replace' if have else 'add'}/{name}", want)
    if status != 200:
        raise RuntimeError(f"MediaMTX refused path {name}: HTTP {status}")


def published_for(info):
    """Seconds since MediaMTX path `info` (its API entry) started publishing; a missing or odd timestamp counts as long enough."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(info["readyTime"].replace("Z", "+00:00"))).total_seconds()
    except (KeyError, ValueError, TypeError):
        return 1e9


def shrink(frame, width):
    """Frame scaled to `width`: halved with INTER_AREA (its fast path) while still 2x too wide, then LINEAR. As smooth as
    INTER_AREA straight to the size, which costs ~67 ms of CPU per 5 MP frame (1.7 cores at 25 fps), against ~10 ms."""
    h, w = frame.shape[:2]
    out = (width, round(h * width / w / 2) * 2)
    while frame.shape[1] >= 2 * width:
        frame = cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2), interpolation=cv2.INTER_AREA)
    return cv2.resize(frame, out, interpolation=cv2.INTER_LINEAR)


class KeyframeReader:
    """cv2.VideoCapture look-alike that decodes keyframes only (ffmpeg -skip_frame nokey), one PPM frame per grab()."""

    def __init__(self, url):
        rtsp = ["-rtsp_transport", "tcp", "-timeout", "10000000"] if url.lower().startswith("rtsp://") else []   # stalled camera -> ffmpeg exits -> reconnect
        self.proc = subprocess.Popen([FFMPEG, "-nostdin", "-loglevel", "error", *rtsp, "-skip_frame", "nokey", "-i", url,
                                      "-an", "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", "ppm", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.frame = None

    def isOpened(self):
        return self.proc.poll() is None

    def get(self, prop):
        return 0

    def set(self, prop, value):
        return False

    def grab(self):
        import numpy as np
        out = self.proc.stdout
        head = [out.readline() for _ in range(3)]   # P6 / width height / 255
        try:
            w, h = map(int, head[1].split())
        except ValueError:
            return False
        data = out.read(w * h * 3)
        if len(data) != w * h * 3:
            return False
        self.frame = cv2.cvtColor(np.frombuffer(data, np.uint8).reshape(h, w, 3), cv2.COLOR_RGB2BGR)
        return True

    def retrieve(self):
        return self.frame is not None, self.frame

    def release(self):
        self.proc.kill()
        self.proc.wait()


def next_playout(recent, last, now, delay, max_backlog=3):
    """Frame (arrived, seq, image) the viewer shows now, or None. Plays unseen frames in order once they are `delay` old,
    one per tick, so a burst of frames from the network comes out evenly; if more than max_backlog wait, jump to the newest."""
    ready = [r for r in recent if r[1] > last and now - r[0] >= delay]
    if not ready:
        return None
    return ready[-1] if len(ready) > max_backlog else ready[0]


class Camera:
    def __init__(self, cfg, source, index):
        self.cfg, self.source, self.index = cfg, source, index
        self.conn, self.url_version = None, 0   # conn = IP/credentials set from the website; wins over source
        self.lock = threading.RLock()
        self.counter = LineCounter(cfg["line"], cfg["in_side"])
        self.error = None   # why the last line-counting check failed, shown on the website (the server log is out of reach)
        self.frame = None
        self.frame_seq = self.infer_seq = 0
        self.dets, self.people, self.visible = [], 0, 0   # people = crowd count (tracked count for inout)
        self.status = "starting"
        self.due = self.crowd_due = self.detect_due = 0.0
        self.yolo_n = self.crowd_total = self.crowd_raw = None   # detector count / crowd-model count (calibrated, median of the last few) / its latest raw output; people is chosen from them
        self.crowd_hist = deque(maxlen=max(1, COUNT_WINDOW))
        self.yolo_at = 0.0
        self.busy = self.crowd_busy = False     # a worker is handling this camera; stops two workers racing on one tracker
        self.times = deque(maxlen=20)
        self.crowd_seq, self.counted_at, self.watch_until = 0, None, 0.0   # watch_until: someone is viewing this camera
        self.hub_base, self.hub, self.hub_native = None, False, False   # hub: analysis reads our MediaMTX path; hub_native: the source already is one
        self.registered = set()                                         # MediaMTX paths this app created for the camera (id, id_hd)
        self.frame_at = self.hub_since = self.hub_retry_at = self.viewed_until = 0.0   # last frame / hub switched on / do not retry the hub before / camera open in the viewer
        self.grabs = self.reconnects = 0   # decoded frames / re-opens: the health check turns these into source fps and drop-outs
        self.size = None                   # (width, height) the camera sends
        self.keyframes = False             # reading keyframes only (KeyframeReader): an idle density camera
        self.view_on, self.view_until = False, 0.0   # view_loop is publishing <id>_view (H.264) to MediaMTX, until view_until
        self.recent = deque(maxlen=10)   # (arrived, seq, frame) while watched: the viewer's de-jitter buffer
        self.jpegs = {}                  # width -> (key, jpeg): viewers and tiles of one frame share one encode

    # ---- capture -------------------------------------------------------------------
    def set_status(self, status):
        with self.lock:
            self.status = status

    def url(self):
        if self.hub:
            return f"{self.hub_base}/{self.cfg['id']}"   # one session to the NVR: MediaMTX pulls it once and serves both analysis and viewers
        return connection_url(self.conn) if self.conn else self.source

    def hub_sources(self):
        """MediaMTX paths to create for this camera, {path: (rtsp url, on demand)}: camNN = the stream we analyse, camNN_hd = its main stream."""
        cid = self.cfg["id"]
        if self.conn:
            out = {cid: (connection_url(self.conn), False)}
            hd = main_path(self.conn["path"])
            if hd:
                out[cid + "_hd"] = (connection_url({**self.conn, "path": hd}), True)
            return out
        return {} if self.hub_native or not self.source.lower().startswith("rtsp://") else {cid: (self.source, False)}

    def play_path(self, hd=False):
        """MediaMTX path the browser can play over WebRTC, or None (then the viewer uses MJPEG)."""
        if not WHEP_URL or not (self.hub or (self.hub_native and not self.conn)):   # a camera with typed-in IP is on MediaMTX only once registered
            return None
        cid = self.cfg["id"] + ("_hd" if hd else "")
        return cid if not hd or cid in self.registered else None

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
            # MJPEG viewer open (watch_until) needs every frame; HLS viewers get video from MediaMTX and need nothing from here
            keyframes = bool(FFMPEG) and role == "density" and not is_file and time.monotonic() >= self.watch_until
            if cap is not None and keyframes != self.keyframes:
                cap.release()
                cap = None
            if role == "off":
                if cap is not None:
                    cap.release()
                    cap = None
                self.set_status("disabled")
                pause(1)
                continue
            if cap is None:
                # timeouts: a hung NVR must end in a reconnect, not a frozen frame that still says "live"
                self.keyframes = keyframes
                cap = KeyframeReader(self.url()) if keyframes else cv2.VideoCapture(
                    self.url(), cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.set_status("offline")
                    pause(5)
                    continue
                file_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                self.reconnects += 1
                with self.lock:
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
            self.grabs += 1
            if is_file:
                time.sleep(1 / file_fps)  # files read unthrottled otherwise
            now = time.monotonic()
            watched = now < self.watch_until   # watched: keep every frame, uneven skipping is what made the video jump
            if not watched and self.recent:
                with self.lock:
                    self.recent.clear()
            if watched or now - last >= min(0.5, 1 / FPS[role]) * 0.6:  # otherwise only the frames analysis can use; 0.6 because frame ticks (40 ms at 25 fps) would round the rate down
                ok, frame = cap.retrieve()
                if ok:
                    last, self.size = now, (frame.shape[1], frame.shape[0])
                    with self.lock:
                        self.frame, self.frame_seq, self.status, self.frame_at = frame, self.frame_seq + 1, "live", now
                        if watched:
                            self.recent.append((now, self.frame_seq, frame))

    def ensure_view(self):
        """Someone plays <id>_view: keep the H.264 encoder running for a few more seconds (its playlist is polled ~1/s)."""
        self.view_until = time.monotonic() + 15
        with self.lock:
            if self.view_on or not FFMPEG or not self.hub_base:
                return
            self.view_on = True
        threading.Thread(target=self.view_loop, name=f"view-{self.cfg['id']}", daemon=True).start()

    def view_loop(self):
        """H.264 for browsers that cannot play the camera's H.265: one ffmpeg reads the camera from MediaMTX, shrinks it to
        VIEW_WIDTH and publishes <id>_view. Frames never pass through Python (that capped it at ~8 fps under the GIL and starved
        line counting), and the camera's own timestamps are kept, so it plays at the camera's frame rate."""
        proc = None
        try:
            while time.monotonic() < self.view_until:
                if proc is None or proc.poll() is not None:
                    if proc is not None:   # source dropped: retry shortly while still watched
                        time.sleep(2)
                    url = self.url()
                    src = (["-rtsp_transport", "tcp", "-timeout", "10000000"] if url.lower().startswith("rtsp://")
                           else [] if url.lower().startswith(STREAM_PREFIXES) else ["-re", "-stream_loop", "-1"])
                    proc = subprocess.Popen(
                        [FFMPEG, "-nostdin", "-loglevel", "error", "-threads", "4", *src, "-i", url, "-an",
                         "-vf", f"scale='min(iw,{VIEW_WIDTH})':-2", "-c:v", "libx264", "-preset", "veryfast",
                         "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-threads", "2",
                         "-force_key_frames", "expr:gte(t,n_forced)",   # a keyframe every 1 s = MediaMTX's 1 s HLS segments
                         "-crf", "26", "-maxrate", "2500k", "-bufsize", "5000k",
                         "-f", "rtsp", "-rtsp_transport", "tcp", f"{self.hub_base}/{self.cfg['id']}_view"],
                        stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
        except OSError as exc:
            log.warning("%s: H.264 view stopped: %s", self.cfg["name"], exc)
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()
            with self.lock:
                self.view_on = False

    # ---- inference -----------------------------------------------------------------
    def infer(self, model):
        """Line crossings (inout / both): detect people, then LineCounter matches them to the last check's and counts."""
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        h, w = frame.shape[:2]
        started = time.monotonic()
        boxes = model.predict(frame, classes=[0], conf=INOUT_CONF, imgsz=INOUT_IMGSZ, verbose=False)[0].boxes.xyxy.cpu().numpy()
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:  # role changed mid-inference; drop the result
                return
            self.infer_seq = seq
            self.dets = self.counter.update([tuple(map(float, b)) for b in boxes], w, h, now)
            self.visible = len(self.dets)
            self.error = None
            if role == "inout":
                self.people = self.visible
            self.times.append(now)
            self.due = started + 1 / TRACK_FPS   # period from the start of this check, not its end: otherwise 5 fps becomes ~4

    def detector_count(self):
        """Caller holds the lock. The detector's count, or None when it is too old to trust over the crowd model."""
        return self.yolo_n if time.monotonic() - self.yolo_at < 3 * DETECT_INTERVAL + 5 else None

    def crowded(self):
        """Caller holds the lock. A far-off crowd the detector mostly misses still counts, via the crowd model's total."""
        return (self.detector_count() or 0) > SPARSE_MAX or (self.crowd_total or 0) > 2 * SPARSE_MAX

    def choose_people(self):
        """Caller holds the lock. Few people: trust the detector. Many: the crowd model (the detector misses people in dense crowds)."""
        seen = self.detector_count()
        if self.crowded():
            self.people = max(round(self.crowd_total or 0), seen or 0)
        else:
            self.people = seen if seen is not None else round(self.crowd_total or 0)

    def detect(self, model):
        """Person-detector head count for density / both cameras."""
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        # iou/agnostic stated: ultralytics keeps the tracker call's iou=0.8, which keeps duplicate boxes (one person counted twice)
        n = len(model.predict(frame, classes=[0], conf=COUNT_CONF, iou=0.55, agnostic_nms=True, max_det=3000, imgsz=640, verbose=False)[0].boxes)
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:
                return
            self.yolo_n, self.yolo_at = n, now
            self.choose_people()
            self.detect_due = now + (2 if now < max(self.watch_until, self.viewed_until) else DETECT_INTERVAL)

    def count_crowd(self, counter):
        """Crowd count from the DM-Count + CSRNet fusion model (density / both)."""
        with self.lock:
            frame, seq, role = self.frame, self.frame_seq, self.cfg["role"]
        raw = counter.count(frame)
        now = time.monotonic()
        with self.lock:
            if self.cfg["role"] != role:
                return
            self.crowd_seq, self.counted_at, self.crowd_raw = seq, now, raw
            self.crowd_hist.append(max(0.0, raw * self.cfg["count_scale"] + self.cfg["count_bias"]))
            self.crowd_total = statistics.median(self.crowd_hist)
            self.choose_people()
            self.crowd_due = now + CROWD_INTERVAL

    # ---- views ---------------------------------------------------------------------
    def rate(self):
        with self.lock:
            t = list(self.times)
        if len(t) < 2 or time.monotonic() - t[-1] > max(3, 3 / TRACK_FPS):
            return 0.0
        return round((len(t) - 1) / (t[-1] - t[0]), 2) if t[-1] > t[0] else 0.0

    def stats(self, thresholds):
        with self.lock:
            cfg, people, status = self.cfg, self.people, self.status
            counts = self.counter.counts()
            tracked_at = self.times[-1] if self.times else None
        counted_at, visible = self.counted_at, self.visible
        with self.lock:
            detected = self.detector_count()
            crowd_total = self.crowd_raw
        out = {"id": cfg["id"], "name": cfg["name"], "zone": cfg["zone"], "location": cfg["location"], "role": cfg["role"],
               "status": status, "people": people, "visible": visible, "fps": self.rate(),
               "resolution": f"{self.size[0]}x{self.size[1]}" if self.size else None,
               "play": self.play_path(), "play_hd": self.play_path(hd=True),
               "hls": self.play_path() if HLS_URL and engine.hls_on else None,
               "hls_hd": self.play_path(hd=True) if HLS_URL and engine.hls_on else None,
               "hls_h264": f"{cfg['id']}_view" if HLS_URL and engine.hls_on and FFMPEG and self.hub_base else None,
               "target_fps": TRACK_FPS if cfg["role"] in TRACKING else 0}
        if cfg["role"] in TRACKING:
            out.update(counts, error=self.error,
                       tracked_ago=None if tracked_at is None else round(time.monotonic() - tracked_at, 1))
        if cfg["role"] in DENSITY:
            area = cfg["area_m2"] or ground_area(cfg)
            density = round(people / area, 2) if area else None
            level = None if density is None else (
                "critical" if density >= thresholds["critical"] else
                "warning" if density >= thresholds["warning"] else "safe")
            out.update(area_m2=round(area, 1) if area else None, density=density, level=level,
                       counted_ago=None if counted_at is None else round(time.monotonic() - counted_at),
                       detected=detected, crowd_model_count=None if crowd_total is None else round(crowd_total, 1))
        return out

    def render(self, width=None, item=None):
        """JPEG of a frame (default: the latest) with the count line and the count only (boxes with SHOW_BOXES=1)."""
        with self.lock:
            seq, frame = (item[1], item[2]) if item else (self.frame_seq, self.frame)
            cfg, dets = self.cfg, list(self.dets)
            counts, people = self.counter.counts(), self.people
        if frame is None:
            return None
        label = {"inout": f"IN {counts['in']}  OUT {counts['out']}",
                 "both": f"IN {counts['in']}  OUT {counts['out']}  PEOPLE {people}",
                 "density": f"PEOPLE {people}"}.get(cfg["role"], "")
        key = (seq, label, cfg["role"], str(cfg["line"]), cfg["in_side"], dets if SHOW_BOXES else None)
        cached = self.jpegs.get(width)
        if cached and cached[0] == key:
            return cached[1]
        h, w = frame.shape[:2]
        if width and width < w:  # shrink first: the overlay is then drawn on the pixels that are sent
            frame = shrink(frame, width)
        else:
            frame = frame.copy()
        scale = frame.shape[1] / w
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
            x1, y1, x2, y2 = x1 * scale, y1 * scale, x2 * scale, y2 * scale
            cv2.rectangle(frame, (round(x1), round(y1)), (round(x2), round(y2)), GREEN, 2)
            cv2.circle(frame, (round((x1 + x2) / 2), round(y2)), 4, YELLOW, -1)
            cv2.putText(frame, f"#{track_id}", (round(x1), max(20, round(y1) - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
        cv2.putText(frame, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, VIEW_QUALITY])
        jpg = jpg.tobytes() if ok else None
        if len(self.jpegs) > 8:   # ?w= is free-form: keep the cache small
            self.jpegs.clear()
        self.jpegs[width] = (key, jpg)
        return jpg

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
    relay_wake = threading.Event()   # set when a camera's connection changes: sync MediaMTX now instead of at the next 20 s tick
    relay_down = False
    hls_on = False

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
            cam.hub_base, cam.hub_native = self.mediamtx, cam.source.startswith(self.mediamtx + "/")
            cam.conn = validated_connection(raw.get("connection") or {})
            self.cameras[cfg["id"]] = cam
        self.rate_times = deque(maxlen=60)
        self.active_head_jobs = 0
        self.gates_since_head = 0
        self.crowd_model = "loading"
        self.detector_error = None
        self.warnings = []

    def sync_relay(self):
        """Register website-entered cameras with MediaMTX, then point analysis at them. On failure fall back to reading the cameras directly."""
        try:
            status, listing = hub_call("GET", "/v3/config/paths/list?itemsPerPage=1000")
            if status != 200 or not listing:
                raise RuntimeError(f"MediaMTX API answered HTTP {status}")
            existing = {item["name"]: item for item in listing["items"]}
            if HLS_URL and not self.hls_on:   # an older config/mediamtx.yml has HLS off: switch it on here
                self.hls_on = hub_call("PATCH", "/v3/config/global/patch", HLS_SETTINGS)[0] == 200
            for index, cam in enumerate(self.cameras.values()):
                active = index < ACTIVE_CAMERA_COUNT
                wanted = cam.hub_sources()
                for path, (url, on_demand) in wanted.items():
                    hub_ensure(path, url, on_demand or not active, existing)
                for path in cam.registered - set(wanted):
                    hub_call("POST", f"/v3/config/paths/delete/{path}")
                cam.registered = set(wanted)
                self.set_hub(cam, active and bool(wanted) and time.monotonic() >= cam.hub_retry_at)
            if self.relay_down:
                log.info("MediaMTX control API reachable again")
            self.relay_down = False
        except (OSError, RuntimeError, ValueError) as exc:
            if not self.relay_down:
                log.warning("MediaMTX unavailable (%s): reading cameras directly", exc)
            self.relay_down = True
            for cam in self.cameras.values():
                self.set_hub(cam, False)
        now = time.monotonic()
        for cam in self.cameras.values():   # the API answers but the stream does not arrive (wrong address, MediaMTX cannot reach the camera): go direct
            if cam.hub and now - max(cam.frame_at, cam.hub_since) > (60 if cam.keyframes else 15):   # keyframes can be seconds apart
                log.warning("%s: no video from MediaMTX for 15 s, reading the camera directly for 10 min", cam.cfg["name"])
                cam.hub_retry_at = now + 600
                self.set_hub(cam, False)

    @staticmethod
    def set_hub(cam, on):
        if cam.hub != on:
            cam.hub, cam.hub_since = on, time.monotonic()
            cam.url_version += 1   # the capture thread reconnects to the new address

    def relay_loop(self):
        while True:
            self.relay_wake.wait(20)
            self.relay_wake.clear()
            self.sync_relay()

    def start(self):
        if RELAY_API:
            self.sync_relay()   # before the capture threads start, so they connect once, to the right address
            threading.Thread(target=self.relay_loop, name="relay", daemon=True).start()
        active = list(self.cameras.values())[:ACTIVE_CAMERA_COUNT]
        for cam in list(self.cameras.values())[ACTIVE_CAMERA_COUNT:]:
            cam.set_status("inactive")  # keep saved settings; no RTSP connection or worker job
        for cam in active:
            threading.Thread(target=cam.capture, name=f"cap-{cam.cfg['id']}", daemon=True).start()
        self.pick_lock = threading.Lock()
        log.info("%d active cameras, %d cores: %d detector workers, %d crowd workers",
                 len(active), CORES, INOUT_WORKERS, CROWD_WORKERS)
        for k in range(INOUT_WORKERS):
            threading.Thread(target=self.infer_loop, name=f"inference-{k}", daemon=True).start()
            time.sleep(0.15)  # avoid all YOLO models allocating their first inference together
        for k in range(CROWD_WORKERS):
            threading.Thread(target=self.crowd_loop, name=f"crowd-{k}", daemon=True).start()
            time.sleep(0.25)  # model checkpoints can temporarily need twice their steady RAM while loading
        threading.Thread(target=self.health_loop, name="health", daemon=True).start()

    def health_loop(self):
        """Every HEALTH_S: say what is wrong and why (offline, slow source, small picture, reconnects, overloaded CPU)."""
        last, t0 = {cid: (c.grabs, c.reconnects) for cid, c in self.cameras.items()}, time.monotonic()
        while True:
            time.sleep(HEALTH_S)
            now = time.monotonic()
            found, offline, span = [], [], now - t0
            for cid, cam in list(self.cameras.items())[:ACTIVE_CAMERA_COUNT]:
                grabs, reconnects = last.get(cid, (cam.grabs, cam.reconnects))
                last[cid] = cam.grabs, cam.reconnects
                if cam.cfg["role"] == "off" or now - t0 < HEALTH_S:
                    continue
                name = cam.cfg["name"]
                if cam.status != "live":
                    offline.append(f"{name}: offline (check IP, password and stream path)")
                    continue
                if cam.size and cam.size[0] > 1920 and not cam.keyframes:
                    found.append(f"{name}: picture is {cam.size[0]}x{cam.size[1]}; decoding it costs the CPU several 1080p streams (a sub-stream path is lighter)")
                fps = (cam.grabs - grabs) / span
                if fps < 8 and not cam.keyframes:   # keyframes only: ~1 a second is expected
                    found.append(f"{name}: camera sends only {fps:.0f} fps (network, or the camera's frame-rate setting)")
                if cam.size and cam.size[0] < 640:
                    found.append(f"{name}: picture is {cam.size[0]}x{cam.size[1]}; use a sub-stream of at least 640x360")
                if cam.reconnects - reconnects >= 3:
                    found.append(f"{name}: reconnected {cam.reconnects - reconnects} times (unstable link)")
                if cam.cfg["role"] in TRACKING and (cam.error or not cam.times):
                    found.insert(0,f"{name}: line counting {'failed: ' + cam.error if cam.error else 'has not run yet'}")
                elif cam.cfg["role"] in TRACKING and cam.rate() < 5:
                    found.append(f"{name}: line counting at {cam.rate():.1f} checks/s, below 5 (CPU short: add cores or detector workers)")
                if cam.cfg["role"] in DENSITY and cam.counted_at and now - cam.counted_at > 2 * CROWD_INTERVAL + 10:
                    found.append(f"{name}: head count is {now - cam.counted_at:.0f} s old (CPU short: add crowd workers)")
            found += offline   # last: with many unplugged cameras they would fill the 20 shown
            t0, self.warnings = now, found[:20]
            for line in found:
                log.warning("health: %s", line)

    def pick_detector_job(self):
        """Reserve detector capacity for gates while still servicing due density counts."""
        now = time.monotonic()
        with self.pick_lock:
            cams = [c for c in list(self.cameras.values())[:ACTIVE_CAMERA_COUNT] if not c.busy and c.frame is not None]
            heads = [c for c in cams if c.cfg["role"] in DENSITY and c.detect_due <= now]
            gates = [c for c in cams if c.cfg["role"] in TRACKING and c.frame_seq != c.infer_seq and c.due <= now]
            # Density checks can become overdue together. Let at most one run
            # alongside gate jobs, and give it a turn every eight gate checks.
            if gates and (not heads or self.active_head_jobs or self.gates_since_head < 8):
                job = ("gate", min(gates, key=lambda c: c.due))
            elif heads and not self.active_head_jobs:
                job = ("count", min(heads, key=lambda c: c.detect_due))
            else:
                return None
            job[1].busy = True
            if job[0] == "count":
                self.active_head_jobs += 1
                self.gates_since_head = 0
            else:
                self.gates_since_head += 1
            return job

    def infer_loop(self):
        while True:
            try:
                from ultralytics import YOLO
                import numpy as np
                import torch
                log.info("Loading people model %s", MODEL_PATH)
                model = YOLO(MODEL_PATH)  # one model per worker thread: ultralytics predictors are not thread-safe
                model.predict(np.zeros((64, 64, 3), np.uint8), imgsz=INOUT_IMGSZ, verbose=False)
                torch.set_num_threads(1)
                self.detector_error = None
                break
            except Exception as exc:
                self.detector_error = f"{type(exc).__name__}: {exc}"[:200]
                log.exception("Gate detector failed to start; retrying")
                time.sleep(15)
        while True:
            job = self.pick_detector_job()
            if job is None:
                time.sleep(0.02)
                continue
            kind, cam = job
            try:
                if kind == "gate":
                    cam.infer(model)
                    self.rate_times.append(time.monotonic())
                else:
                    cam.detect(model)
            except Exception as exc:
                log.exception("%s failed for %s", "Inference" if kind == "gate" else "Head count", cam.cfg["id"])
                if kind == "gate":
                    cam.error = f"{type(exc).__name__}: {exc}"[:200]
                    cam.due = time.monotonic() + 5
                else:
                    cam.detect_due = time.monotonic() + 30
            finally:
                with self.pick_lock:
                    cam.busy = False
                    if kind == "count":
                        self.active_head_jobs -= 1

    def pick_crowd_job(self):
        now = time.monotonic()
        with self.pick_lock:
            ready = [c for c in list(self.cameras.values())[:ACTIVE_CAMERA_COUNT]
                     if c.cfg["role"] in DENSITY and c.frame is not None and c.crowd_due <= now and not c.crowd_busy]
            if not ready:
                return None
            cam = min(ready, key=lambda c: c.crowd_due)
            cam.crowd_busy = True
            return cam

    def crowd_loop(self):
        """Counts crowds in its own low-priority process (one per worker), so the video and gate tracking never lag."""
        worker = None
        while True:
            cam = None
            try:
                if worker is None or worker.poll() is not None:
                    worker = CrowdProcess()
                    self.crowd_model = "DM-Count + CSRNet fusion" + (" (learned head)" if worker.mode == "learned" else "")
                cam = self.pick_crowd_job()
                if cam is None:
                    time.sleep(0.5)
                    continue
                cam.count_crowd(worker)
            except Exception as exc:
                log.exception("Crowd count failed%s", f" for {cam.cfg['id']}" if cam else "")
                if cam is not None:
                    cam.crowd_due = time.monotonic() + 30
                else:
                    self.crowd_model = f"unavailable: {exc}"
                if worker is not None:
                    worker.close()          # a failed count means a broken pipe: restart the worker
                worker = None
                time.sleep(5 if cam is not None else 30)
            finally:
                if cam is not None:
                    cam.crowd_busy = False

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
        cams = [c.stats(self.thresholds) for c in list(self.cameras.values())[:ACTIVE_CAMERA_COUNT]]
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
                           "crowd_model": self.crowd_model,
                           "warnings": (["Gate detector failed to start: " + self.detector_error]
                                        if getattr(self, "detector_error", None) else []) + self.warnings,
                           "cpu": {"cores": CORES, "detector_workers": INOUT_WORKERS, "crowd_workers": CROWD_WORKERS,
                                   "load": [round(x, 1) for x in os.getloadavg()]}}}


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
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/authz", "/static/monitor/img/ap_police_logo.png"}  # logo is shown on the login page
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


@app.get("/authz")
def authorize_hls():
    """Internal gateway subrequest: authorize one HLS path without returning its bytes through Python."""
    if session.get("user") != AUTH[0]:
        abort(401)
    path = request.headers.get("X-Original-URI", "").split("?", 1)[0]
    match = re.fullmatch(r"/hls/([A-Za-z0-9_-]+?)(?:_view)?/[A-Za-z0-9_.-]{1,80}\.(?:m3u8|mp4)", path)
    if match is None or match.group(1) not in list(engine.cameras)[:ACTIVE_CAMERA_COUNT]:
        abort(403)
    if "_view/" in path:   # the H.264 copy: every request keeps its encoder alive; its first playlist waits until ~9 s of video exist, so the player can start 10 s behind live
        cam = engine.cameras[match.group(1)]
        cam.ensure_view()
        give_up, need = time.monotonic() + 25, 9 if path.endswith("/index.m3u8") else 0
        while path.endswith(".m3u8") and time.monotonic() < give_up:
            try:
                status, info = hub_call("GET", f"/v3/paths/get/{match.group(1)}_view")
            except OSError:
                break
            if status == 200 and info and info.get("ready") and published_for(info) >= need:
                break
            time.sleep(0.5)
    return "", 204


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.get("/api/cameras")
def list_cameras():
    return jsonify(engine.summary())


@app.get("/api/cameras/<cam_id>")
def get_camera(cam_id):
    cam = cam_or_404(cam_id)
    if time.monotonic() > cam.viewed_until:
        cam.detect_due = 0.0                  # count right away when the viewer opens
    cam.viewed_until = time.monotonic() + 3   # the viewer polls this: head counts every 2 s while it is open
    return jsonify(detail(cam))


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
            cam.dets, cam.people, cam.visible, cam.due = [], 0, 0, 0.0
            cam.counted_at, cam.crowd_due, cam.detect_due = None, 0.0, 0.0
            cam.yolo_n = cam.crowd_total = cam.crowd_raw = None
            cam.crowd_hist.clear()
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
                cam.dets, cam.frame, cam.status = [], None, "starting"
        engine.save()
    engine.relay_wake.set()
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


@app.route("/webrtc/<path:rest>", methods=["POST", "DELETE"])
def webrtc(rest):
    """WHEP (WebRTC video) signalling to MediaMTX, behind this app's login. Only the offer/answer passes here; the video flows browser <-> MediaMTX."""
    m = re.fullmatch(r"([A-Za-z0-9_-]{1,64})/whep(/[0-9a-f-]{36})?", rest)
    cam = engine.cameras.get(m.group(1).removesuffix("_hd")) if m else None
    if not WHEP_URL or cam is None:
        abort(404)
    req = urllib.request.Request(f"{WHEP_URL}/{rest}", method=request.method, data=request.get_data() or None,
                                 headers={"Content-Type": request.content_type or "application/sdp"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            status, body, headers = res.status, res.read(), res.headers
    except urllib.error.HTTPError as exc:
        status, body, headers = exc.code, exc.read(), exc.headers
    except OSError:
        abort(502)
    out = Response(body, status=status, mimetype=headers.get("Content-Type", "application/sdp"), headers={"Cache-Control": "no-store"})
    if headers.get("Location"):
        out.headers["Location"] = "/webrtc/" + headers["Location"].split("://", 1)[-1].split("/", 1)[-1].lstrip("/")
    return out


@app.get("/hls/<cam_id>/<name>")
def hls(cam_id, name):
    """LL-HLS playlists and segments from MediaMTX, behind this app's login (blocking playlist reloads wait up to a few s)."""
    cam = engine.cameras.get(cam_id.removesuffix("_view"))
    if not HLS_URL or cam is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.(m3u8|mp4)", name):
        abort(404)
    if cam_id.endswith("_view"):   # every playlist / segment request keeps the H.264 encoder alive
        cam.ensure_view()
    url = f"{HLS_URL}/{cam_id}/{name}" + (f"?{request.query_string.decode()}" if request.query_string else "")
    give_up = time.monotonic() + (20 if cam_id.endswith("_view") else 0)   # the H.264 copy takes a few s to start; hls.js never retries a 404
    while True:
        try:
            with urllib.request.urlopen(url, timeout=20) as res:
                status, body, ctype = res.status, res.read(), res.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            status, body, ctype = exc.code, exc.read(), exc.headers.get("Content-Type")
        except OSError:
            abort(502)
        if status != 404 or time.monotonic() > give_up:
            break
        cam.ensure_view()
        time.sleep(0.5)
    return Response(body, status=status, mimetype=ctype or "application/octet-stream", headers={"Cache-Control": "no-store"})


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
        last, gap, ready_at = -1, 0.8 / VIEW_FPS, 0.0
        cam.detect_due = 0.0   # count right away when the viewer opens
        while True:
            now = time.monotonic()
            cam.watch_until = now + 3        # keeps the camera decoding every frame while this stream is open
            with cam.lock:
                recent = list(cam.recent)
                if not recent and cam.frame is not None:  # first pass: nothing buffered yet, show the latest frame
                    recent = [(0.0, cam.frame_seq, cam.frame)]
            item = next_playout(recent, last, now, VIEW_DELAY) if now >= ready_at else None
            if item is None:
                time.sleep(0.004)
                continue
            last, ready_at = item[1], now + gap   # frames keep the camera's own rhythm; a burst is spread at least `gap` apart
            jpg = cam.render(VIEW_WIDTH, item)
            if jpg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"

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
