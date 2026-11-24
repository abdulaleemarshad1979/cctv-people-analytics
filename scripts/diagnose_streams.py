"""Report source codec, resolution and advertised FPS for active cameras without exposing RTSP credentials.

Run in the analytics container: python scripts/diagnose_streams.py
The browser viewer separately reports actual delivered frames per second.
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ACTIVE_CAMERA_COUNT, RELAY_API, Engine  # noqa: E402


def inspect(cam, relay):
    # Probe the shared MediaMTX session when available; don't open a second NVR
    # connection just to print stream metadata. Never print the source URL.
    url = f"{relay}/{cam.cfg['id']}" if relay and cam.conn else cam.url()
    command = ["ffprobe", "-v", "error", "-rw_timeout", "6000000"]
    if url.lower().startswith("rtsp://"):
        command += ["-rtsp_transport", "tcp"]
    command += ["-select_streams", "v:0", "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate", "-of", "json", url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
        streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
        if not streams:
            return cam.cfg["name"], "no video received (check NVR path or network)"
        video = streams[0]
        rate = Fraction(video.get("avg_frame_rate") or "0/1")
        return cam.cfg["name"], (f"{video.get('codec_name', 'unknown')}  "
                                 f"{video.get('width', '?')}x{video.get('height', '?')}  "
                                 f"source {float(rate):.1f} fps")
    except (OSError, ValueError, ZeroDivisionError, subprocess.TimeoutExpired):
        return cam.cfg["name"], "probe timed out or invalid stream"


if __name__ == "__main__":
    engine = Engine()  # reads camera settings only; starts no capture or model workers
    active = list(engine.cameras.values())[:ACTIVE_CAMERA_COUNT]
    with ThreadPoolExecutor(max_workers=min(3, len(active) or 1)) as pool:
        for name, result in pool.map(lambda cam: inspect(cam, engine.mediamtx if RELAY_API else None), active):
            print(f"{name}: {result}")
