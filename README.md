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
python3 -m unittest     # automated tests
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

- **Count line (all 40 cameras):** every camera in `cameras.json` has its own line, direction and calibration, editable from its tile; the line is used when the role is In / Out or Both. *Edit count line* opens a large paused picture with the current line. Drag either yellow circle to move an end, drag the line to move both ends, then press *Save line*. *Flip IN direction* swaps IN/OUT (the arrow shows which way is IN). Place the line across the actual walking entrance, away from stalls and standing people. Existing totals are kept until you press *Reset counts*.
- **Density:** enter the measured area in m², or enter lens FOV, tilt and mounting height (or click *Estimate camera height* and mark one person's head and feet). Without either, density shows "–" and only the head count is reported.

**Full screen:** open a camera and press the corner button, double-click the video, or press `F` (Esc leaves full screen). The viewer shows the camera name and count only.

## Models

- **Gate crossings:** the `active_pixel_html_camera_site` flow: person detection with `conf=0.35` (`INOUT_CONFIDENCE`), each foot point (bottom-centre of the box) joined to the nearest point of the last check within 0.2 of the frame, and one count when that path crosses the drawn line segment (0.012 dead-band either side, 0.5 s per person between counts), in `people_analytics.py`. No tracker library. The detector is `yolov8n.pt` at `INOUT_IMGSZ` (640). If counting fails, the reason shows in the header warning and as `error` in `/api/cameras`.
- **Crowd count and density:** `dm_count/`, `csrnet/`, `fusion/` are copied from `CCTV_MONITOR`; the ~150 MB weights are read from `../CCTV_MONITOR/` (override with `CCTV_MONITOR_DIR`, `DM_COUNT_WEIGHTS`, `CSRNET_WEIGHTS`, `FUSION_HEAD_WEIGHTS`). Input is 512×288 as in CCTV_MONITOR's CPU mode; its broadcast-overlay filter and flip TTA are not used. If the weights are missing, the header says so and Density cameras stay at 0.

## Speed and accuracy

The video is kept clean and cheap: the count line plus the count only, with no heat maps, boxes or track IDs (set `SHOW_BOXES=1` to draw boxes while tuning a gate). The dashboard stays smooth because analysis never blocks it:

- **Live video (MediaMTX):** visible tiles (up to 16) try LL-HLS of the camera's own stream, then WebRTC, then the shared H.264 HLS copy (limited to eight tiles to protect the CPU). Snapshots remain the last fallback. An opened camera uses LL-HLS and can play its **main stream** through WebRTC when the NVR provides one. The video and inference run independently: smooth playback does not prove that counting is fast enough.
- **One session per camera:** cameras entered on *Connect cameras* are registered in MediaMTX by the app (control API), and analysis reads them from MediaMTX too, so the NVR serves each stream once however many people watch. If MediaMTX is unreachable the app reads the cameras directly again.
- **MJPEG fallback viewer:** up to 25 fps (`VIEW_FPS`), frames encoded once and shared between viewers; `VIEW_DELAY_MS` (default 0) can hold frames to spread network bursts, at that much extra delay. On this machine's looping test stream the fallback measured 24 fps with the longest gap 100-120 ms, no better or worse than before, so it is a fallback, not the smooth path.
- **A server for the cameras only:** the cores are split by core count: detector workers (~35 % of cores, `INOUT_WORKERS`, ~28 line-count checks/s each), crowd-model workers (~25 %, `CROWD_WORKERS`, one count per ~3-5 s each) and the rest for video decode and the website. Each worker uses one torch thread, the most CPU-efficient size. The crowd workers run at the lowest priority (nice 19, below the detectors at 10), so when CPU is short, head counts yield first. (On the 4-core test laptop this did not change the gate rate: it was already limited by the CPU the detectors got.)
- **Line counting (In / Out):** each In/Out and Both camera targets `INOUT_FPS` checks per second (default 10). Overdue density detector checks are limited to one at a time while gates are waiting; they still get a turn every eight gate checks. The actual rate is shown in the camera editor. A line must cross the real entrance path without passing through stalls or standing areas. At 352×288 the people are too small for reliable detection; use a sub-stream at least 640 pixels wide and check a five-minute manual crossing count against IN/OUT before relying on totals. No accuracy percentage is guaranteed without real footage and a hand-counted comparison.
- **Head count every 30 s:** every Density and Both camera gets a crowd-model count every `CROWD_INTERVAL_S` (default 30) at the model's 512×288 input (`CROWD_INFER_WIDTH/HEIGHT` raise it: counts of dense scenes rise at larger sizes, but with no hand-counted frames here we do not know which size is right; it costs 2-3x the CPU).
- **Few people in view:** the crowd model is built for dense crowds and reads one close-up person as 1.5-4. So Density and Both cameras also run the person detector (every `DETECT_INTERVAL_S`, default 30 s, and every 2 s while the camera is open in the viewer). The detector's count is shown only when it sees at most `SPARSE_MAX` people (default 10) and the crowd model at most twice that; otherwise the crowd model's, never below the detector's. A detector count older than ~95 s is ignored.
- **Video quality:** live tiles use the camera's HLS stream when available. If live playback fails, grid tiles refresh JPEG snapshots up to ~3 times a second, each only after its last picture arrived; analysis uses the sub-stream the camera is configured with.
- **Sizing for ~36 cameras (estimates from an 8-core machine):** decode ~2 cores, website ~1, crowd counts every 30 s ~5, each In/Out camera at 10 checks/s ~0.5. With 10 In/Out cameras plan ~20 cores; with all 36 doing both line and head counts plan ~32-40 cores. RAM (32 GB) and disk are not limits. The editor shows each gate's actual rate against its target, and a tile turns amber ("slow") when it falls behind.
- **Test laptop caveat:** the machine behind the numbers below is an i7-1185G7 laptop (4 cores, 8 threads, throttled clocks), not an 8-core server. One detector check costs 35-70 ms alone and about 150 ms while 40 cameras and 2 crowd workers run; 3 detector threads in one process give 38 checks/s when nothing else runs, so threads scale and the limit is CPU. Plan cores from the table above, not from this laptop.
- **Measured on this 8-thread, no-GPU machine** (40 looping 640×360 files standing in for cameras) with 4 In/Out + 36 head-count cameras: 3 detector workers gave ~5 checks/s per gate (target 10) and 2 crowd workers needed ~90 s for a round of 36 counts (target 30 s). That is the expected shortfall for 8 cores; with more cores both targets should be met, but this has not been run on a bigger machine.

## Live video setup (WebRTC)

- Cameras must send **H.264 without B-frames** (set the stream to H.264, Baseline/Main profile; not H.265 / "H.264+" / Smart codec). Anything else still works through the MJPEG fallback, with less quality and more server load.
- The browser must reach this server on **port 8189, UDP and TCP** (published by `docker-compose.yml`), besides the dashboard port. `WEBRTC_HOSTS` in `.env` lists the addresses browsers use for this server (LAN IP, Tailscale IP, DNS name); `install.sh` fills in this machine's addresses. After changing it: `sudo docker compose up -d`.
- Existing installs: run `install.sh` again (it replaces an untouched `config/mediamtx.yml`); if you generated that file with `gen_mediamtx.py`, run it again. Browsers load nothing from the internet (no STUN, no CDN).
- Behind a reverse proxy, forward `/webrtc/` like any other path. The video itself does not pass through the proxy; port 8189 must be reachable directly.

## Counting accuracy

- **Head count:** the crowd model's number is corrected per camera (`count_scale`, `count_bias`, default 1 and 0), the linear calibration from `CCTV_MONITOR`; the detector's count is still the floor. `COUNT_TEMPORAL_WINDOW=3` also takes the median of the last 3 counts (one bad frame cannot move the total, but a real surge shows 30-60 s later); it is off by default, as in `CCTV_MONITOR` on CPU.
- **Reaching 90 % needs your own footage:** nobody can promise a percentage without hand counts. For each camera, write down the "crowd model" number shown in its viewer and your own count of the same frame, 3 or more times (different crowd sizes), into a CSV `camera,model,actual`, then run `python3 scripts/calibrate_counts.py counts.csv`. It prints the accuracy now and after the fitted correction (accuracy = 100 − mean |model − actual| / max(actual, 1)); `--write config/cameras.json` stores the correction; restart the app to apply it. Do the same for gates: stand and count crossings for 5 minutes against `IN`/`OUT`.
- **Health warnings:** every 30 s (`HEALTH_CHECK_S`) the app logs and shows in the header what is wrong and why: camera offline, camera sending under 8 fps, picture under 640 px wide, repeated reconnects, line counting under 5 checks/s, head count older than 70 s. A hung camera now ends in a reconnect instead of a frozen frame.

## Known limits

- Counts live in memory: they reset on server restart, or with *Reset counts* per camera.
- The fusion model was built for crowds; on sparse scenes or screens/posters it can over-count. Check each camera against a manual count.
- Density per m² assumes a level floor with people spread evenly across the visible ground; use the area override for obstructed views.
- The WebRTC video has no burned-in overlay: the count and line are drawn by the browser from the live numbers (refreshed ~0.7 s in an opened camera, 2 s in the grid). Snapshots and the MJPEG fallback are annotated by the server.
- Not tested against real cameras or an NVR: WebRTC, the MediaMTX registration and the count correction were run on this machine with a looping test stream. Try one real camera first.

## API

`GET /api/cameras` (summary + totals) · `GET /api/cameras/<id>` · `POST /api/cameras/<id>/config` · `POST /api/cameras/<id>/calibrate-height` · `POST /api/cameras/<id>/reset-counts` · `GET /api/cameras/<id>/frame.jpg?w=480` · `/snapshot.jpg` · `/stream.mjpg`
