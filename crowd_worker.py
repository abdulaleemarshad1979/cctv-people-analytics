"""Crowd-count worker process: loads the DM-Count + CSRNet fusion model and counts frames sent over stdin.

Runs at low CPU priority in its own process so the heavy model never slows the video, the gate tracker or
the web server. Protocol: parent writes "<height> <width>\\n" + raw BGR bytes; worker answers one JSON line.
"""
import json
import os
import sys

os.nice(10)
os.environ.setdefault("CROWD_THREADS", "2")   # leaves cores for the website and gate tracking                                   # the website and gate tracking always win the CPU
out = sys.stdout.buffer
sys.stdout = sys.stderr                       # model loaders print(); keep stdout for the protocol only

import numpy as np                            # noqa: E402
from crowd_model import CrowdCounter          # noqa: E402

counter = CrowdCounter()
out.write((json.dumps({"ready": True, "mode": counter.mode}) + "\n").encode())
out.flush()
inp = sys.stdin.buffer
while True:
    header = inp.readline()
    if not header:
        break                                  # parent closed the pipe
    h, w = map(int, header.split())
    buf = inp.read(h * w * 3)
    if len(buf) != h * w * 3:
        break
    total, _ = counter.count(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
    out.write((json.dumps({"total": total}) + "\n").encode())
    out.flush()
