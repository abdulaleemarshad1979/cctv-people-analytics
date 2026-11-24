"""Report source codec, resolution, advertised and measured FPS, and the keyframe interval for active cameras without exposing RTSP credentials.

A long keyframe interval (many seconds) makes a video start late: the browser copy has to wait for the next keyframe.

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


def measure(command_head, url):
    """(delivered fps, keyframe every X s on average, longest gap) over ~8 s of the stream, or None."""
    command = command_head + ["-select_streams", "v:0", "-read_intervals", "%+8", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", url]
    try:
        rows = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False).stdout.split()
        packets = [(float(t), "K" in f) for t, f in (r.split(",")[:2] for r in rows if "," in r and r.split(",")[0] not in ("", "N/A"))]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if len(packets) < 2 or packets[-1][0] <= packets[0][0]:
        return None
    keys = [t for t, key in packets if key]
    gaps = [b - a for a, b in zip(keys, keys[1:])]
    return ((len(packets) - 1) / (packets[-1][0] - packets[0][0]),
            sum(gaps) / len(gaps) if gaps else None, max(gaps) if gaps else None)


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
        line = (f"{video.get('codec_name', 'unknown')}  {video.get('width', '?')}x{video.get('height', '?')}  "
                f"advertised {float(rate):.1f} fps")
        got = measure(command[:command.index("-select_streams")], url)
        if got is None:
            return cam.cfg["name"], line + "  (could not measure frames)"
        fps, key_avg, key_max = got
        line += f"  delivered {fps:.1f} fps"
        if key_avg is not None:
            line += f"  keyframe every {key_avg:.1f} s (longest {key_max:.1f} s)"
            if key_max > 4:
                line += "  <-- long: videos start late; set the camera's I-frame interval to 1-2 s"
        return cam.cfg["name"], line
    except (OSError, ValueError, ZeroDivisionError, subprocess.TimeoutExpired):
        return cam.cfg["name"], "probe timed out or invalid stream"


if __name__ == "__main__":
    engine = Engine()  # reads camera settings only; starts no capture or model workers
    active = list(engine.cameras.values())[:ACTIVE_CAMERA_COUNT]
    with ThreadPoolExecutor(max_workers=min(3, len(active) or 1)) as pool:
        for name, result in pool.map(lambda cam: inspect(cam, engine.mediamtx if RELAY_API else None), active):
            print(f"{name}: {result}")
