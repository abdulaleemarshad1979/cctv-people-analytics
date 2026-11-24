"""Person-detector worker process: finds people in frames sent over stdin, away from the web server's Python process.

One of these per detector thread of app.py. YOLO's own Python work (pre/post-processing) no longer competes with the web
server, the video and the other cameras for one interpreter lock, so the line-count rate stays steady and rises with cores.
Protocol: parent writes one JSON line {"h", "w", "args": {predict kwargs}} + raw BGR bytes; worker answers {"boxes": [[x1,y1,x2,y2],...]}.
"""
import json
import os
import sys

out = sys.stdout.buffer
sys.stdout = sys.stderr                       # libraries print(); keep stdout for the protocol only

import numpy as np                            # noqa: E402
import torch                                  # noqa: E402
from ultralytics import YOLO                  # noqa: E402

torch.set_num_threads(int(os.getenv("INOUT_TORCH_THREADS", "2")))
model = YOLO(os.getenv("PEOPLE_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolov8n.pt")))
model.predict(np.zeros((64, 64, 3), np.uint8), verbose=False)   # first call allocates: do it before "ready"
out.write(b'{"ready": true}\n')
out.flush()
inp = sys.stdin.buffer
while True:
    header = inp.readline()
    if not header:
        break                                  # parent closed the pipe
    job = json.loads(header)
    h, w = job["h"], job["w"]
    buf = inp.read(h * w * 3)
    if len(buf) != h * w * 3:
        break
    result = model.predict(np.frombuffer(buf, np.uint8).reshape(h, w, 3), classes=[0], verbose=False, **job["args"])[0]
    out.write((json.dumps({"boxes": result.boxes.xyxy.cpu().numpy().round(1).tolist()}) + "\n").encode())
    out.flush()
