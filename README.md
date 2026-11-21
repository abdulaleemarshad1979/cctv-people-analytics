# CCTV People Counting & Crowd Density

## Install on an Ubuntu server (Docker, one command)

```bash
curl -fsSL https://raw.githubusercontent.com/abdulaleemarshad1979/cctv-people-analytics/main/install.sh | sudo bash
```

This installs Docker if missing, clones the repo to `/opt/cctv-people-analytics`, downloads the DM-Count and CSRNet weights (checksum-verified), and starts two containers: **MediaMTX** (RTSP hub) and the **analytics dashboard**. It prints the dashboard URL and the login: user `admin`, password `admin123`. **Change the password** with `sudo /opt/cctv-people-analytics/scripts/set-password.sh` (asks for the new one) or `sudo /opt/cctv-people-analytics/scripts/set-password.sh 'NewPassword'`. The first build takes a few minutes (PyTorch). Run the same command again to update.

Then connect your cameras **from the website**: sign in, click **Connect cameras** (top bar), and either

- fill one NVR's details (IP, port, username, password, first camera, number of cameras) and press **Fill the table below**: camera 1 gets channel 1, camera 2 channel 2, and so on (CP PLUS / Dahua and Hikvision paths built in, or a custom path with `{ch}`), or
- type each camera's IP, port, username, password and stream path in the table (cameras on different NVRs or standalone IP cameras).

Press **Test** on a row to check the connection, then **Save all changes**; the cameras reconnect within seconds. Passwords are stored in `config/cameras.json` (mode 600) and are never sent back to the browser. The server must be able to reach the camera IPs (e.g. over your Tailscale subnet route). Use each NVR's **sub-stream**.

Optional, if you prefer a MediaMTX relay between NVR and app (cameras left blank in the table use `rtsp://mediamtx:8554/camNN`):

```bash
cd /opt/cctv-people-analytics
sudo nano config/nvrs.json                       # NVR address, user, password, channels
sudo python3 scripts/gen_mediamtx.py             # writes config/mediamtx.yml: cam01.. = NVR channels (sub-stream)
sudo docker compose restart mediamtx
```

With 40 cameras on several NVRs, fill the table once per NVR (e.g. cameras 1-12 from the first NVR, 13-24 from the next), or list each NVR in `nvrs.json` with its own `first_camera`. Set each camera's role, location, name and count line from its tile in the dashboard.

| Task | Command (in `/opt/cctv-people-analytics`) |
|---|---|
| Logs | `sudo docker compose logs -f analytics` |
| Restart | `sudo docker compose restart` |
| Stop | `sudo docker compose down` |
| Change port, login or tuning | edit `.env`, then `sudo docker compose up -d` |

Your camera list and settings live in `config/` and weights in `weights/`; neither is overwritten by updates. The image is CPU-only. The dashboard has a login page (cookie session, 12 h, with a Logout button); the default `admin` / `admin123` is public in this repo, so change it. Do not expose port 5000 to the internet; keep it on your LAN/VPN, and put it behind HTTPS if it leaves a trusted network.


One dashboard for up to ~40 CCTV cameras. Each camera has a **role** and a **location**, both editable from the UI (click a tile):

| Role | What it does |
|---|---|
| **In / Out** | Tracks people and counts each one when their feet cross a line you draw (same logic as `Count peoplr`). |
| **Density** | Counts the crowd with the **DM-Count + CSRNet fusion** model from `CCTV_MONITOR` (80 % / 20 % blend, or the learned head if `fusion_head.pth` exists), and reports people per m² with a Safe / Warning / Critical level. |
| **Both** | Does both on the same camera: crossings plus head count and density. |
| **Off** | Stops decoding that camera. |

| Location | Meaning |
|---|---|
| **Inside** / **Outside** | Whether the camera watches inside or outside the temple / ghat. The top bar splits people into *Inside* and *Outside* (plus the total), with each side's gate IN / OUT. |

Each side shows its crowd count, its density (people ÷ monitored area, for cameras that have an area) and its gate IN / OUT. "People" is the fusion crowd count from Density and Both cameras. In/Out-only cameras add to Gate IN / OUT but not to the crowd count, so two cameras that overlap the same crowd are double-counted; give overlapping views to only one Density camera.

The front end reuses the `CCTV_MONITOR` look: same header, logos and dignitary photos, pills, tiles and dark full-view.

## Where the video comes from

```
CP PLUS NVR (192.168.1.10, 12 cams) → Tailscale gateway → encrypted VPN → Ubuntu server → MediaMTX (RTSP) → this app
```

Each camera is read from `rtsp://<mediamtx>/<id>` (default `rtsp://127.0.0.1:8554/cam01` … `cam40`). Point MediaMTX paths at the NVR **sub-streams** (640×360 is plenty for counting; main streams will saturate the CPU on decode).

## Run

```bash
cd ~/Work/Analytics
python3 -m pip install -r requirements.txt
python3 app.py          # http://127.0.0.1:5000
python3 -m unittest     # 11 tests
```

`WEB_HOST=0.0.0.0` exposes it to the network. The login defaults to `admin` / `admin123` (`LOGIN_USER`, `LOGIN_PASSWORD`); change it before exposing the app.

## Configure cameras

Edit `cameras.json` (the UI also saves role, name, zone, line and calibration back to it):

```json
{"mediamtx": "rtsp://127.0.0.1:8554",
 "thresholds": {"warning": 2.0, "critical": 4.0},
 "cameras": [{"id": "cam01", "name": "Gate 1 IN/OUT", "zone": "Zone A", "role": "inout"},
             {"id": "cam09", "name": "Plaza", "zone": "Zone B", "role": "density", "area_m2": 150,
              "source": "rtsp://user:pass@192.168.1.10:554/ch9"}]}
```

- `source` is optional and overrides the MediaMTX path. It can only be set in this file, never through the API, and it is never sent to the browser. If it holds a password, keep the file private (`chmod 600`; saves keep the permissions).
- `role` is `inout`, `density`, `both` or `off`; `location` is `inside` or `outside`.
- Defaults (change them to match your site): cam01–cam04 In/Out, cam05–cam40 Density, all `outside`. Change them in the UI or the file.
- Thresholds are people per m² (2 = Warning, 4 = Critical).

## Setting up a camera (click a tile)

- **Count line (all 40 cameras):** every camera in `cameras.json` has its own line, direction and calibration, editable from its tile; the line is used when the role is In / Out or Both. *Edit count line* opens a snapshot with the current line. Drag either round handle to move or extend that end, drag the line itself to move it, *Extend to edges* stretches it to the frame border, *Reset line* restores the default, *Flip IN direction* swaps IN/OUT (the yellow arrow shows which way is IN). *Save line* applies it; totals are kept.
- **Density:** enter the measured area in m², or enter lens FOV, tilt and mounting height (or click *Estimate camera height* and mark one person's head and feet). Without either, density shows "–" and only the head count is reported.

**Full screen:** open a camera and press the corner button, double-click the video, or press `F` (Esc leaves full screen). The viewer shows the camera name and count only.

## Models

- **Gate crossings:** the `Count peoplr` algorithm: person detection with `conf=0.12, iou=0.8`, ByteTrack IDs, and the foot-point line crossing in `people_analytics.py` (unchanged). The detector here is `yolov8n.pt` at 480 px for speed; set `PEOPLE_MODEL=yolo26s-seg.pt` (as in `Count peoplr`) and `INOUT_IMGSZ=960` for its exact model, about 4× slower per frame.
- **Crowd count and density:** `dm_count/`, `csrnet/`, `fusion/` are copied from `CCTV_MONITOR`; the ~150 MB weights are read from `../CCTV_MONITOR/` (override with `CCTV_MONITOR_DIR`, `DM_COUNT_WEIGHTS`, `CSRNET_WEIGHTS`, `FUSION_HEAD_WEIGHTS`). Input is 512×288 as in CCTV_MONITOR's CPU mode; its broadcast-overlay filter and flip TTA are not used. If the weights are missing, the header says so and Density cameras stay at 0.

## Speed and accuracy

The video is kept clean and cheap: the count line plus the count only, with no heat maps, boxes or track IDs (set `SHOW_BOXES=1` to draw boxes while tuning a gate). The dashboard stays smooth because analysis never blocks it:

- **Live viewer:** an open camera is decoded at ~12 fps (`VIEW_FPS`); other cameras are only decoded at the rate analysis needs. Tiles are refreshed snapshots.
- **Crowd model in its own process:** DM-Count + CSRNet runs in `crowd_worker.py` at low CPU priority, so it cannot slow the video, the gate tracker or the website. Gate tracking waits for nothing; crowd counting waits while a gate lags (`GATE_YIELD_RATIO`).
- **Gates:** counting is only reliable at ≥ 5 checks/s. On a walker crossing 10 % of the frame per second, the counted share was ~17 % at 2/s, 43 % at 4/s and ~75 % at 5/s or more (synthetic clip: treat as approximate and test each gate on its own footage). Defaults: `INOUT_FPS=5`, `INOUT_IMGSZ=480`.
- **Crowd:** one count per Density camera every `CROWD_INTERVAL_S` seconds (default 60), at the model's 512×288 input (`CROWD_INFER_WIDTH/HEIGHT` to raise it for accuracy at a CPU cost).
- **Measured on this 8-core, no-GPU machine** with 4 gates + 36 crowd cameras: live viewer ~10 fps, pages and tiles load in milliseconds, gates ~3.4/s (4.4/s when crowd counting is idle) and a full crowd round in about 4 minutes. For accurate counts on 4 gates and 36 crowd cameras you need a GPU or fewer cameras; the editor shows each gate's actual rate against its target and a tile turns amber ("slow") when it falls behind.

## Known limits

- Counts live in memory: they reset on server restart, or with *Reset counts* per camera.
- The fusion model was built for crowds; on sparse scenes or screens/posters it can over-count. Check each camera against a manual count.
- Density per m² assumes a level floor with people spread evenly across the visible ground; use the area override for obstructed views.
- Tiles show a snapshot refreshed every 3 s; live annotated video streams only in the opened camera view.

## API

`GET /api/cameras` (summary + totals) · `GET /api/cameras/<id>` · `POST /api/cameras/<id>/config` · `POST /api/cameras/<id>/calibrate-height` · `POST /api/cameras/<id>/reset-counts` · `GET /api/cameras/<id>/frame.jpg?w=480` · `/snapshot.jpg` · `/stream.mjpg`
