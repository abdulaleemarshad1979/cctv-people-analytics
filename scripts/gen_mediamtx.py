#!/usr/bin/env python3
"""Write a MediaMTX config that pulls each NVR channel as camNN.

Usage: python3 scripts/gen_mediamtx.py [nvrs.json] [out.yml]
nvrs.json is a list of {host, port, user, password, channels, first_camera, subtype}. The URL form is the
Dahua/CP PLUS one: rtsp://user:pass@host:554/cam/realmonitor?channel=N&subtype=1 (subtype 1 = sub-stream).
Use sub-streams: 640x360 is enough for counting and keeps CPU decode low.
"""
import json
import os
import sys
from urllib.parse import quote

src = sys.argv[1] if len(sys.argv) > 1 else "config/nvrs.json"
dst = sys.argv[2] if len(sys.argv) > 2 else "config/mediamtx.yml"
nvrs = json.load(open(src))
lines = ["logLevel: info", "rtsp: yes", "rtspAddress: :8554", "protocols: [tcp]", "rtmp: no", "hls: no",
         "webrtc: no", "srt: no", "api: no", "paths:", "  all_others:"]
used = set()
for nvr in nvrs:
    auth = f"{quote(nvr['user'], safe='')}:{quote(nvr['password'], safe='')}"
    for i in range(nvr["channels"]):
        cam = f"cam{nvr['first_camera'] + i:02d}"
        if cam in used:
            sys.exit(f"{cam} is defined twice; check first_camera/channels in {src}")
        used.add(cam)
        url = f"rtsp://{auth}@{nvr['host']}:{nvr.get('port', 554)}/cam/realmonitor?channel={i + 1}&subtype={nvr.get('subtype', 1)}"
        lines += [f"  {cam}:", f"    source: {json.dumps(url)}", "    sourceOnDemand: no"]
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds NVR passwords
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"{len(used)} cameras written to {dst}")
