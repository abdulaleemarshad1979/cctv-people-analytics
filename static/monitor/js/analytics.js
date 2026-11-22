"use strict";
const $ = (sel, root = document) => root.querySelector(sel);
const grid = $("#grid");
const tiles = new Map();      // camera id -> {el, img, loading}
const inView = new Set();     // ids of tiles currently on screen
let data = null, filter = "all", zone = "", loc = "", openId = null, drawMode = null, points = [], photoUrl = null, cur = null, line = null;

const observer = new IntersectionObserver(entries => {
  for (const e of entries) (e.isIntersecting ? inView.add(e.target.dataset.id) : inView.delete(e.target.dataset.id));
}, {rootMargin: "200px"});

const fmt = v => (v == null ? "–" : v);
const text = (root, sel, value) => { $(sel, root).textContent = value; };

async function api(url, body) {
  const res = await fetch(url, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || "Request failed");
  return json;
}

// ---------- Grid ----------
function makeTile(c) {
  const el = document.createElement("article");
  el.className = "tile"; el.dataset.id = c.id;
  el.innerHTML = `<div class="tile__header"><span class="tile__name"></span><div class="tile__meta">
      <span class="tile__zone"></span><span class="tile__loc"></span><span class="tile__role"></span><span class="tile__status-dot"></span></div></div>
    <div class="tile__body"><video class="tile__video" muted playsinline></video><img class="tile__img" alt=""><div class="tile__placeholder"></div></div>
    <div class="tile__metrics">
      <div class="metric"><small></small><strong></strong></div><div class="metric"><small></small><strong></strong></div>
      <div class="metric"><small></small><strong></strong></div><div class="metric"><small></small><strong></strong></div></div>`;
  el.tabIndex = 0; el.setAttribute("role", "button"); el.setAttribute("aria-label", "Open " + c.name);
  el.addEventListener("click", () => openModal(c.id));
  el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openModal(c.id); } });
  observer.observe(el);
  grid.appendChild(el);
  const t = {el, img: $(".tile__img", el), video: $(".tile__video", el), loading: false, stop: null, retryAt: 0, cam: c};
  tiles.set(c.id, t);
  return t;
}

const tracks = c => c.role === "inout" || c.role === "both";
const dense = c => c.role === "density" || c.role === "both";
const ROLE_LABEL = {inout: "In/Out", density: "Density", both: "Both", off: "Off"};

function metricsFor(c) {
  if (c.role === "inout") return [["In", c.in], ["Out", c.out], ["Net", c.in - c.out]];
  if (c.role === "density") return [["People", c.people], ["Per m²", c.density], ["Level", null]];
  if (c.role === "both") return [["In", c.in], ["Out", c.out], ["People", c.people], ["Level", null]];
  return [];
}

function updateTile(t, c) {
  const el = t.el;
  t.cam = c;
  el.dataset.status = c.status; el.dataset.role = c.role; el.dataset.level = c.level || ""; el.dataset.loc = c.location;
  text(el, ".tile__name", c.name); text(el, ".tile__zone", c.zone); text(el, ".tile__loc", c.location);
  const slow = tracks(c) && c.status === "live" && c.fps < 0.8 * c.target_fps;
  el.dataset.slow = slow ? "1" : "";
  text(el, ".tile__role", ROLE_LABEL[c.role] + (slow ? " · slow" : ""));
  text(el, ".tile__placeholder", c.status === "live" ? "" : c.status);
  if (c.status !== "live") t.img.removeAttribute("src");
  const cells = el.querySelectorAll(".metric"), rows = metricsFor(c);
  el.querySelector(".tile__metrics").style.setProperty("--n", rows.length || 1);
  cells.forEach((cell, i) => {
    cell.classList.toggle("hidden", i >= rows.length);
    if (i >= rows.length) return;
    const [label, value] = rows[i];
    $("small", cell).textContent = label;
    const strong = $("strong", cell);
    if (label === "Level") {
      strong.innerHTML = "";
      const badge = document.createElement("span");
      badge.className = "zone-badge " + (c.level || ""); badge.textContent = c.level || "n/a";
      strong.appendChild(badge);
    } else strong.textContent = fmt(value);
  });
  const byRole = filter === "all" || filter === "offline" || c.role === filter
    || (filter === "inout" && c.role === "both") || (filter === "density" && c.role === "both");
  const hidden = !byRole || (filter === "offline" && c.status === "live") || (zone && c.zone !== zone) || (loc && c.location !== loc);
  el.classList.toggle("hidden", !!hidden);
}

function render() {
  const {cameras, totals} = data;
  for (const c of cameras) updateTile(tiles.get(c.id) || makeTile(c), c);
  $("#empty").classList.toggle("hidden", !!grid.querySelector(".tile:not(.hidden)"));
  const zones = [...new Set(cameras.map(c => c.zone).filter(Boolean))].sort();
  const select = $("#zone");
  if ([...select.options].slice(1).map(o => o.value).join("|") !== zones.join("|")) {
    select.innerHTML = '<option value="">All zones</option>';
    for (const z of zones) select.add(new Option(z, z));
    select.value = zone;
  }
  const L = totals.locations;
  $("#t-people").textContent = totals.people;
  $("#t-ins").textContent = L.inside.people; $("#t-outs").textContent = L.outside.people;
  for (const [id, l] of [["ins", L.inside], ["outs", L.outside]])
    $(`#t-${id}-sub`).textContent = `${fmt(l.density)} /m² · IN ${l.in} / OUT ${l.out}`;
  $("#t-in").textContent = totals.in; $("#t-out").textContent = totals.out;
  $("#t-net").textContent = `Net ${totals.in - totals.out}`;
  $("#t-gates").textContent = `${cameras.filter(tracks).length} In/Out cameras`;
  $("#t-peak").textContent = fmt(totals.peak_density);
  $("#t-peak-cam").textContent = totals.peak_camera ? totals.peak_camera + " · people / m²" : "people / m²";
  $("#t-online").textContent = `${totals.online}`; $("#t-total").textContent = `of ${totals.total}`;
  $("#infer").textContent = `Gate tracking ${totals.infer_per_s}/s · Crowd model: ${totals.crowd_model}` + (totals.warnings.length ? ` · ⚠ ${totals.warnings[0]}` + (totals.warnings.length > 1 ? ` (+${totals.warnings.length - 1} more)` : "") : "");
  if (openId) updateLive(cameras.find(c => c.id === openId));
}

async function poll() {
  try { data = await api("/api/cameras"); render(); } catch (e) { $("#infer").textContent = "Disconnected"; }
}

// Tiles show a refreshed snapshot (browsers cap ~6 connections/host, so 40 live MJPEG streams would stall).
function refreshImages() {
  for (const [id, t] of tiles) {
    if (t.loading || t.el.dataset.rtc === "1" || !inView.has(id) || t.el.dataset.status !== "live" || t.el.classList.contains("hidden")) continue;
    t.loading = true;
    const probe = new Image();
    probe.onload = () => { t.img.src = probe.src; t.loading = false; };
    probe.onerror = () => { t.loading = false; };
    probe.src = `/api/cameras/${id}/frame.jpg?w=480&t=${Date.now()}`;
  }
}

// Live video in the grid: WebRTC for the tiles on screen (at most MAX_RTC at once, none while a camera is open).
// A tile whose video cannot start keeps its refreshed snapshot and tries again after 30 s.
const MAX_RTC = 16;
function stopTile(t) { if (t.stop) t.stop(); t.stop = null; t.el.dataset.rtc = ""; }
function manageRtc() {
  let running = 0;
  for (const [id, t] of tiles) {
    const want = !openId && t.cam && t.cam.play && t.cam.status === "live" && inView.has(id) && !t.el.classList.contains("hidden");
    if (!want && t.stop) stopTile(t);
    if (t.stop) running++;
  }
  for (const [id, t] of tiles) {
    if (running >= MAX_RTC) break;
    if (t.stop || Date.now() < t.retryAt || openId || !t.cam || !t.cam.play || t.cam.status !== "live" || !inView.has(id) || t.el.classList.contains("hidden")) continue;
    running++;
    t.stop = playWhep(t.video, t.cam.play, (ev, why) => {
      if (ev === "playing") t.el.dataset.rtc = "1";
      else { console.warn(`[video] ${id}: ${why}; using snapshots`); t.stop = null; t.el.dataset.rtc = ""; t.retryAt = Date.now() + 30000; }
    });
  }
}

$("#filters").addEventListener("click", e => {
  const pill = e.target.closest(".pill"); if (!pill) return;
  filter = pill.dataset.filter;
  document.querySelectorAll(".pill").forEach(p => p.classList.toggle("active", p === pill));
  if (data) render();
});
$("#zone").addEventListener("change", e => { zone = e.target.value; if (data) render(); });
$("#loc").addEventListener("change", e => { loc = e.target.value; if (data) render(); });

// ---------- Camera editor ----------
const panelHTML = `
  <h3>Live</h3><div class="live" id="p-live"></div>
  <div class="note" id="p-rate"></div>
  <h3>Camera</h3>
  <label for="f-name">Name</label><input id="f-name" maxlength="60">
  <label for="f-zone">Zone</label><input id="f-zone" maxlength="60">
  <label for="f-role">Role</label>
  <select id="f-role"><option value="inout">In / Out counting</option><option value="density">Crowd count + density</option><option value="both">Both (In/Out + density)</option><option value="off">Off</option></select>
  <label for="f-loc">Location</label>
  <select id="f-loc"><option value="inside">Inside temple / ghat</option><option value="outside">Outside temple / ghat</option></select>
  <div id="p-inout"><h3>Count line</h3>
    <label for="f-side">Direction</label>
    <select id="f-side"><option value="positive">Direction A is IN</option><option value="negative">Direction B is IN</option></select>
    <button class="btn" id="b-line">Edit count line</button><button class="btn danger" id="b-reset">Reset counts</button>
    <div id="p-line-edit" hidden><p class="note">Drag the round handles to move or extend an end, or drag the line to move it. Save when done.</p>
      <button class="btn" id="b-line-save">Save line</button><button class="btn ghost" id="b-flip">Flip IN direction</button>
      <button class="btn ghost" id="b-extend">Extend to edges</button><button class="btn ghost" id="b-line-default">Reset line</button></div>
    <p class="note">Every camera has its own line; it is used when the role is In / Out or Both. Place the line across the walking path. A person counts when their feet (yellow dot) cross it. The yellow IN arrow on the video shows which way counts as IN; switch A/B if it points the wrong way. Counts reset only on request or server restart.</p></div>
  <div id="p-density"><h3>Area &amp; calibration</h3>
    <div class="row"><div><label for="f-vfov">Vertical FOV (°)</label><input id="f-vfov" type="number" step="0.1"></div>
      <div><label for="f-hfov">Horizontal FOV (°)</label><input id="f-hfov" type="number" step="0.1"></div>
      <div><label for="f-tilt">Downward tilt (°)</label><input id="f-tilt" type="number" step="0.1"></div>
      <div><label for="f-person">Person height (m)</label><input id="f-person" type="number" step="0.01"></div></div>
    <label for="f-height">Camera mounting height (m)</label><input id="f-height" type="number" step="0.01" placeholder="Estimate or enter measured value">
    <button class="btn ghost" id="b-height">Estimate camera height</button>
    <label for="f-area">Monitored area override (m²)</label><input id="f-area" type="number" step="1" placeholder="Blank = computed from calibration">
    <div class="geo" id="p-geo"></div>
    <p class="note">Density = people in view ÷ visible ground area, assuming a level floor and people spread evenly. Enter the measured area for crowded or obstructed views.</p></div>
  <div style="margin-top:14px"><button class="btn" id="b-save">Save settings</button><button class="btn ghost" id="b-cancel" hidden>Cancel drawing</button></div>
  <div id="m-msg"></div>`;

const val = id => $("#" + id).value;
const num = id => (val(id) === "" ? null : Number(val(id)));
const msg = t => { $("#m-msg").textContent = t; };

function updateLive(c) {
  if (!c) return;
  drawHud(c);
  const cells = $("#p-live").children;
  const slow = tracks(c) && c.status === "live" && c.fps < 0.8 * c.target_fps;
  const parts = [];
  if (tracks(c)) parts.push(`Tracking ${c.fps}/s (target ${c.target_fps}/s)` + (slow ? " – too slow, crossings may be missed" : ""));
  if (dense(c)) parts.push(c.counted_ago == null ? "Crowd count pending" : `Crowd count ${c.counted_ago} s ago`,
    `detector ${c.detected ?? "–"} · crowd model ${c.crowd_model_count ?? "–"}`);
  $("#p-rate").textContent = parts.join(" · ");
  $("#p-rate").style.color = slow ? "#fbbf24" : "#94a3b8";
  const rows = c.role === "inout" ? [["In", c.in], ["Out", c.out], ["Visible", c.people]]
    : c.role === "density" ? [["People", c.people], ["Per m²", c.density], ["Level", c.level || "n/a"]]
    : c.role === "both" ? [["In", c.in], ["Out", c.out], ["People", c.people], ["Per m²", c.density]]
    : [["Status", c.status]];
  const box = $("#p-live");
  box.style.setProperty("--n", rows.length);
  box.replaceChildren(...rows.map(([label, value]) => {
    const cell = document.createElement("div"), small = document.createElement("small"), strong = document.createElement("strong");
    small.textContent = label; strong.textContent = fmt(value);
    cell.append(small, strong);
    return cell;
  }));
}

function showRole(role) {
  $("#p-inout").classList.toggle("hidden", role === "off");  // every camera keeps its own line; used when role is In/Out or Both
  $("#p-density").classList.toggle("hidden", role !== "density" && role !== "both");
  $("#m-badge").textContent = {inout: "In / Out", density: "Density", both: "Both", off: "Off"}[role];
}

function fill(d) {
  const cfg = d.config, active = document.activeElement;
  cur = cfg;
  if (!active || !["INPUT", "SELECT"].includes(active.tagName)) {
    $("#f-name").value = cfg.name; $("#f-zone").value = cfg.zone; $("#f-role").value = cfg.role; $("#f-loc").value = cfg.location; $("#f-side").value = cfg.in_side;
    $("#f-vfov").value = cfg.vertical_fov_deg; $("#f-hfov").value = cfg.horizontal_fov_deg; $("#f-tilt").value = cfg.tilt_deg;
    $("#f-person").value = cfg.reference_height_m; $("#f-height").value = cfg.camera_height_m ?? ""; $("#f-area").value = cfg.area_m2 ?? "";
  }
  const g = d.geometry, m = v => (v == null ? "beyond horizon" : v.toFixed(1) + " m");
  $("#p-geo").textContent = g ? `Ground range ${g.near_m == null ? "outside view" : m(g.near_m)} to ${m(g.far_m)}` + (d.area_m2 ? ` · area ≈ ${d.area_m2} m²` : "") : "Calibrate to estimate coverage.";
  $("#m-title").textContent = cfg.name + (cfg.zone ? " · " + cfg.zone : "");
  $("#m-fs-title").textContent = $("#m-title").textContent;
  showRole(cfg.role); updateLive(d);
}

async function openModal(id) {
  openId = id;
  $("#m-panel").innerHTML = panelHTML;
  $("#modal").classList.remove("hidden");
  startViewer(id);
  liveTimer = setInterval(async () => { try { const d = await api(`/api/cameras/${id}`); updateLive(d); } catch (e) { /* next tick */ } }, 700);
  $("#f-role").onchange = () => showRole(val("f-role"));
  $("#b-save").onclick = () => save().then(() => msg("Saved.")).catch(e => msg(e.message));
  $("#b-reset").onclick = () => api(`/api/cameras/${id}/reset-counts`, {}).then(() => msg("Counts reset.")).catch(e => msg(e.message));
  $("#b-line").onclick = startLine;
  $("#b-line-save").onclick = saveLine;
  $("#b-flip").onclick = () => { $("#f-side").value = val("f-side") === "positive" ? "negative" : "positive"; drawLine(); };
  $("#b-extend").onclick = extendLine;
  $("#b-line-default").onclick = () => { line = [[0.1, 0.5], [0.9, 0.5]]; drawLine(); };
  $("#b-height").onclick = () => startDraw("person");
  $("#b-cancel").onclick = endDraw;
  try { fill(await api(`/api/cameras/${id}`)); } catch (e) { msg(e.message); }
}

function closeModal() {
  if (document.fullscreenElement) document.exitFullscreen();
  if (pseudo) setPseudo(false);
  endDraw(); openId = null;
  stopViewer(); clearInterval(liveTimer);
  $("#modal").classList.add("hidden");
}
$("#m-close").onclick = closeModal; $("#m-backdrop").onclick = closeModal;
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || !openId) return;
  if (pseudo) setPseudo(false); else closeModal();   // first Esc leaves full screen, the next closes the viewer
});

async function save() {
  const d = await api(`/api/cameras/${openId}/config`, {
    name: val("f-name"), zone: val("f-zone"), role: val("f-role"), location: val("f-loc"), in_side: val("f-side"),
    vertical_fov_deg: num("f-vfov"), horizontal_fov_deg: num("f-hfov"), tilt_deg: num("f-tilt"),
    reference_height_m: num("f-person"), camera_height_m: num("f-height"), area_m2: num("f-area")});
  fill(d); poll();
  return d;
}

// ---------- Snapshot editor: draggable count line, click-to-mark camera height ----------
const stage = $("#m-stage"), overlay = $("#m-overlay"), photo = $("#m-photo");
const clamp = v => Math.max(0, Math.min(1, v));

function endDraw() {
  drawMode = null; points = []; line = null;
  setLive(true); stage.style.display = "none"; overlay.innerHTML = ""; overlay.classList.remove("edit");
  stage.querySelectorAll(".handle, .in-label").forEach(el => el.remove());
  if ($("#b-cancel")) { $("#b-cancel").hidden = true; $("#p-line-edit").hidden = true; }
}

async function showSnapshot(mode) {
  await save();
  const res = await fetch(`/api/cameras/${openId}/snapshot.jpg?t=${Date.now()}`);
  if (!res.ok) throw new Error("Wait for a live frame, then try again.");
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  photoUrl = URL.createObjectURL(await res.blob());
  photo.src = photoUrl;
  endDraw();
  drawMode = mode;
  setLive(false); stage.style.display = "block"; $("#b-cancel").hidden = false;
}

async function startDraw(mode) {      // mode "person": click head, then feet
  try { await showSnapshot(mode); msg("Click the top of one person's head, then their feet."); } catch (e) { msg(e.message); }
}

// Line editor: two endpoint handles plus a draggable body. Coordinates stay 0-1 so they match the server.
async function startLine() {
  try {
    await showSnapshot("line");
    line = cur.line.map(p => [...p]);
    overlay.classList.add("edit");
    overlay.innerHTML = `<line id="ln-hit" stroke="transparent" stroke-width="18" vector-effect="non-scaling-stroke"/>
      <line id="ln-vis" stroke="#ffe066" stroke-width="3" vector-effect="non-scaling-stroke"/>
      <polyline id="ln-arrow" fill="none" stroke="#ffe066" stroke-width="3" vector-effect="non-scaling-stroke"/>`;
    for (const i of [0, 1]) {
      const h = document.createElement("div"); h.className = "handle"; stage.appendChild(h);
      drag(h, (o, x, y) => { line[i] = [clamp(x), clamp(y)]; });
    }
    const label = document.createElement("div"); label.className = "in-label"; label.textContent = "IN"; stage.appendChild(label);
    drag($("#ln-hit"), (o, x, y, dx, dy) => {
      const lo = [-Math.min(o[0][0], o[1][0]), -Math.min(o[0][1], o[1][1])];
      const hi = [1 - Math.max(o[0][0], o[1][0]), 1 - Math.max(o[0][1], o[1][1])];
      const mx = Math.max(lo[0], Math.min(hi[0], dx)), my = Math.max(lo[1], Math.min(hi[1], dy));
      line = o.map(p => [p[0] + mx, p[1] + my]);
    });
    $("#p-line-edit").hidden = false;
    msg("Drag the handles or the line, then Save line.");
    drawLine();
  } catch (e) { msg(e.message); }
}

function drag(el, move) {
  el.addEventListener("pointerdown", e => {
    e.preventDefault(); el.setPointerCapture(e.pointerId);
    const r = photo.getBoundingClientRect(), orig = JSON.parse(JSON.stringify(line));
    const pos = ev => [(ev.clientX - r.left) / r.width, (ev.clientY - r.top) / r.height];
    const [sx, sy] = pos(e);
    const onMove = ev => { const [x, y] = pos(ev); move(orig, x, y, x - sx, y - sy); drawLine(); };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", () => el.removeEventListener("pointermove", onMove), {once: true});
  });
}

function drawLine() {
  if (!line) return;
  const r = photo.getBoundingClientRect(), W = r.width, H = r.height, [a, b] = line;
  for (const id of ["ln-hit", "ln-vis"]) {
    const el = $("#" + id);
    el.setAttribute("x1", a[0] * 100); el.setAttribute("y1", a[1] * 100); el.setAttribute("x2", b[0] * 100); el.setAttribute("y2", b[1] * 100);
  }
  // IN arrow: normal of the line on the side the server treats as "positive" (flipped for negative).
  const nx = (a[1] - b[1]) * W, ny = (b[0] - a[0]) * H, n = Math.hypot(nx, ny) || 1;
  const k = val("f-side") === "positive" ? 1 : -1, ux = k * nx / n, uy = k * ny / n;
  const mx = (a[0] + b[0]) / 2 * W, my = (a[1] + b[1]) / 2 * H, tip = [mx + ux * 50, my + uy * 50];
  const barb = ang => [tip[0] + (ux * Math.cos(ang) - uy * Math.sin(ang)) * 14, tip[1] + (ux * Math.sin(ang) + uy * Math.cos(ang)) * 14];
  const S = p => `${p[0] / W * 100},${p[1] / H * 100}`;
  $("#ln-arrow").setAttribute("points", `${S([mx, my])} ${S(tip)} ${S(barb(2.6))} ${S(tip)} ${S(barb(-2.6))}`);
  const handles = stage.querySelectorAll(".handle");
  [a, b].forEach((p, i) => { handles[i].style.left = p[0] * 100 + "%"; handles[i].style.top = p[1] * 100 + "%"; });
  const label = stage.querySelector(".in-label");
  label.style.left = (tip[0] + ux * 14) / W * 100 + "%"; label.style.top = (tip[1] + uy * 14) / H * 100 + "%";
}

// Lengthen the line in both directions until it meets the frame border (Liang-Barsky clip).
function extendLine() {
  const [a, b] = line, d = [b[0] - a[0], b[1] - a[1]];
  let lo = -Infinity, hi = Infinity;
  for (const i of [0, 1]) {
    if (Math.abs(d[i]) < 1e-9) continue;
    const t1 = (0 - a[i]) / d[i], t2 = (1 - a[i]) / d[i];
    lo = Math.max(lo, Math.min(t1, t2)); hi = Math.min(hi, Math.max(t1, t2));
  }
  line = [[clamp(a[0] + d[0] * lo), clamp(a[1] + d[1] * lo)], [clamp(a[0] + d[0] * hi), clamp(a[1] + d[1] * hi)]];
  drawLine();
}

async function saveLine() {
  try {
    fill(await api(`/api/cameras/${openId}/config`, {line, in_side: val("f-side")}));
    endDraw(); msg("Count line saved. Counts continue from the current totals.");
  } catch (e) { msg(e.message); }
}

// ---------- Live viewer: WebRTC from MediaMTX (the camera's main stream when it has one), MJPEG when that cannot start ----------
const hdBroken = new Set();   // cameras whose main stream would not play: sub-stream only
let viewerStop = null, hdStop = null, viewerMode = "mjpeg", liveTimer = null;
const hud = $("#m-hud"), vSub = $("#m-video"), vHd = $("#m-video-hd");
let video = vSub;   // the one on screen: the sub-stream first (starts at once), the main stream once that plays

function setLive(show) {   // the live picture (video or MJPEG) on / off, e.g. off while the snapshot editor is open
  for (const v of [vSub, vHd]) v.style.display = show && viewerMode === "rtc" && v === video ? "block" : "none";
  $("#m-live").style.display = show && viewerMode !== "rtc" ? "block" : "none";
  hud.hidden = !(show && viewerMode === "rtc");
  fitHud();
}

function stopViewer() {
  if (viewerStop) viewerStop();
  if (hdStop) hdStop();
  viewerStop = hdStop = null; viewerMode = "mjpeg"; video = vSub;
  $("#m-live").src = "data:,";   // drops the MJPEG connection
  vSub.style.display = vHd.style.display = "none"; hud.hidden = true;
}

function startViewer(id) {
  stopViewer();
  const c = data && data.cameras.find(x => x.id === id);
  const sub = c && c.play, hd = c && c.play_hd;
  const mjpeg = () => { viewerMode = "mjpeg"; $("#m-live").src = `/api/cameras/${id}/stream.mjpg`; setLive(true); };
  const start = (path, el, onPlaying, onFail) => playWhep(el, path, (ev, why) => {
    if (openId !== id) return;
    if (ev === "playing") onPlaying(); else { console.warn(`[video] ${path}: ${why}`); onFail(); }
  });
  if (!sub && !hd) return mjpeg();
  viewerStop = start(sub || hd, vSub, () => {
    viewerMode = "rtc"; video = vSub; $("#m-live").src = "data:,"; setLive(true);
    // The main stream is pulled from the camera on demand and may not be playable (H.265, B-frames): try it behind the picture
    // that already shows, and swap only once it plays.
    // "playing" fires before MediaMTX rejects a stream it cannot carry (B-frames), so swap only after 2 s of steady frames,
    // and come back to the sub-stream (for good, this session) if the main stream drops later.
    if (sub && hd && !hdBroken.has(id)) hdStop = start(hd, vHd, () => setTimeout(() => {
      if (!hdStop || openId !== id || vHd.getVideoPlaybackQuality().totalVideoFrames < 25) return;
      video = vHd; setLive(true); if (viewerStop) viewerStop(); viewerStop = null;
    }, 2000), () => { hdStop = null; hdBroken.add(id); if (video === vHd && openId === id) startViewer(id); });
  }, () => { viewerStop = null; mjpeg(); });
}

// The HUD (count and count line) sits exactly over the picture: the video is letterboxed, so measure where the picture is.
function fitHud() {
  if (hud.hidden || !video.videoWidth) return;
  const bw = video.clientWidth, bh = video.clientHeight, s = Math.min(bw / video.videoWidth, bh / video.videoHeight);
  const w = video.videoWidth * s, h = video.videoHeight * s;
  Object.assign(hud.style, {left: video.offsetLeft + (bw - w) / 2 + "px", top: video.offsetTop + (bh - h) / 2 + "px", width: w + "px", height: h + "px"});
  hud.dataset.w = w; hud.dataset.h = h;
  if (lastHud) drawHud(lastHud);
}

let lastHud = null;
function drawHud(c) {
  lastHud = c;
  if (hud.hidden) return;
  $("#m-hud-label").textContent = c.role === "inout" ? `IN ${c.in}  OUT ${c.out}` : c.role === "both" ? `IN ${c.in}  OUT ${c.out}  PEOPLE ${c.people}` : c.role === "density" ? `PEOPLE ${c.people}` : "";
  const svg = $("#m-hud-svg"), w = +hud.dataset.w, h = +hud.dataset.h, cfg = c.config || cur;
  svg.innerHTML = "";
  if (!tracks(c) || !cfg || !w) return;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const [[ax, ay], [bx, by]] = cfg.line.map(p => [p[0] * w, p[1] * h]);
  const nx = ay - by, ny = bx - ax, n = Math.hypot(nx, ny) || 1, sign = cfg.in_side === "positive" ? 1 : -1;   // the side LineCounter calls "positive"
  const dx = sign * nx / n, dy = sign * ny / n, mx = (ax + bx) / 2, my = (ay + by) / 2, tx = mx + dx * 45, ty = my + dy * 45;
  const head = `${tx - dx * 14 - dy * 8},${ty - dy * 14 + dx * 8} ${tx},${ty} ${tx - dx * 14 + dy * 8},${ty - dy * 14 - dx * 8}`;
  svg.innerHTML = `<g stroke="#ffe066" stroke-width="2.5" fill="none" stroke-linecap="round"><line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}"/><line x1="${mx}" y1="${my}" x2="${tx}" y2="${ty}"/><polyline points="${head}"/></g>
    <text x="${tx + dx * 8 + 4}" y="${ty + dy * 8 + 5}" fill="#ffe066" font-size="14" font-weight="800" stroke="#000" stroke-width="3" paint-order="stroke">IN</text>`;
}
new ResizeObserver(fitHud).observe($(".full-view-body"));
for (const v of [vSub, vHd]) { v.addEventListener("loadedmetadata", fitHud); v.addEventListener("resize", fitHud); }

// ---------- Full screen ----------
const videoArea = $(".full-view-body"), fsIcon = $("#m-fs-icon");
const ICON_ENTER = "M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5", ICON_EXIT = "M3 8h5V3M21 8h-5V3M3 16h5v5M21 16h-5v5";
let idleTimer = null;

let pseudo = false;   // fallback: fill the window when the browser refuses real full screen (e.g. embedded views)

function setFsState(on) {
  fsIcon.firstElementChild.setAttribute("d", on ? ICON_EXIT : ICON_ENTER);
  $("#m-fs").title = on ? "Exit full screen (F)" : "Full screen (F)";
  wake();
}

function setPseudo(on) { pseudo = on; videoArea.classList.toggle("pseudo-fs", on); setFsState(on); }

function toggleFullscreen() {
  if (document.fullscreenElement) return document.exitFullscreen();
  if (pseudo) return setPseudo(false);
  const request = videoArea.requestFullscreen || videoArea.webkitRequestFullscreen;
  const fallback = () => { if (!document.fullscreenElement) setPseudo(true); };
  if (!request) return fallback();
  try { Promise.resolve(request.call(videoArea)).catch(fallback); } catch (e) { fallback(); }
  setTimeout(fallback, 500);   // some views accept the call but never switch
}

function wake() {            // show the controls, hide them again after 2.5 s without movement
  videoArea.classList.remove("idle");
  clearTimeout(idleTimer);
  if (document.fullscreenElement || pseudo) idleTimer = setTimeout(() => videoArea.classList.add("idle"), 2500);
}

$("#m-fs").onclick = toggleFullscreen;
$("#m-live").addEventListener("dblclick", toggleFullscreen);
vSub.addEventListener("dblclick", toggleFullscreen); vHd.addEventListener("dblclick", toggleFullscreen);
videoArea.addEventListener("mousemove", wake);
document.addEventListener("fullscreenchange", () => setFsState(!!document.fullscreenElement));
document.addEventListener("keydown", e => {
  if ((e.key === "f" || e.key === "F") && openId && !e.ctrlKey && !e.metaKey && !e.altKey
      && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) toggleFullscreen();
});

photo.addEventListener("load", drawLine);
window.addEventListener("resize", drawLine);

stage.onclick = async event => {         // camera-height marking only; the line uses the handles above
  if (drawMode !== "person") return;
  const rect = photo.getBoundingClientRect();
  points.push([clamp((event.clientX - rect.left) / rect.width), clamp((event.clientY - rect.top) / rect.height)]);
  const [a, b] = points;
  overlay.innerHTML = points.map(p => `<circle cx="${p[0] * 100}" cy="${p[1] * 100}" r=".7" fill="#ffe066"/>`).join("") +
    (b ? `<line x1="${a[0] * 100}" y1="${a[1] * 100}" x2="${b[0] * 100}" y2="${b[1] * 100}" stroke="#ffe066" stroke-width=".4"/>` : "");
  if (!b) return;
  try {
    fill(await api(`/api/cameras/${openId}/calibrate-height`, {head_y: a[1], foot_y: b[1]}));
    endDraw(); msg("Camera height estimated.");
  } catch (e) { points = []; overlay.innerHTML = ""; msg(e.message + " Click again."); }
};

// ---------- Connect cameras: IP and credentials for every camera ----------
const srcModal = $("#src-modal"), srcRows = $("#src-rows");
let sources = [];            // [{id, name, host, port, user, path, has_password}]
const touched = new Map();   // camera id -> {password?: string} for rows edited in the table

const PRESETS = {
  dahua: (ch, main) => `/cam/realmonitor?channel=${ch}&subtype=${main ? 0 : 1}`,
  hik: (ch, main) => `/Streaming/Channels/${ch}0${main ? 1 : 2}`,
};

function srcMsg(text, bad = true) { const m = $("#src-msg"); m.textContent = text; m.style.color = bad ? "#b45309" : "#15803d"; }

function buildSourceTable() {
  srcRows.replaceChildren();
  for (const c of sources) {
    const tr = document.createElement("tr"); tr.dataset.id = c.id;
    const first = document.createElement("td");
    first.textContent = c.name; const id = document.createElement("small"); id.textContent = c.id; first.appendChild(id);
    tr.appendChild(first);
    const field = (key, type = "text", cls = "") => {
      const td = document.createElement("td"), input = document.createElement("input");
      input.type = type; input.dataset.key = key; input.autocomplete = type === "password" ? "new-password" : "off";
      if (cls) input.className = cls;
      input.addEventListener("input", () => markDirty(c.id, key, input.value));
      td.appendChild(input); tr.appendChild(td); return input;
    };
    const host = field("host"), port = field("port", "number", "c-port"), user = field("user"), pass = field("password", "password"), path = field("path");
    host.placeholder = "blank = use MediaMTX"; host.value = c.host;
    port.value = c.port || 554; user.value = c.user; path.value = c.path; path.placeholder = "/cam/realmonitor?channel=1&subtype=1";
    pass.placeholder = c.has_password ? "saved" : "";
    const check = document.createElement("td"); check.className = "c-check";
    const btn = document.createElement("button"); btn.type = "button"; btn.className = "btn ghost"; btn.textContent = "Test";
    const out = document.createElement("span"); out.className = "test-out"; out.style.marginLeft = "6px";
    btn.addEventListener("click", () => testRow(c.id, btn, out));
    check.append(btn, out); tr.appendChild(check);
    srcRows.appendChild(tr);
  }
}

function markDirty(id, key, value) {
  const entry = touched.get(id) || {};
  if (key === "password") entry.password = value;
  touched.set(id, entry);
  srcRows.querySelector(`tr[data-id="${id}"]`).classList.add("dirty");
}

function rowValues(id) {
  const tr = srcRows.querySelector(`tr[data-id="${id}"]`), get = k => $(`[data-key="${k}"]`, tr).value;
  const row = {id, host: get("host").trim(), port: Number(get("port") || 554), user: get("user"), path: get("path").trim() || "/"};
  const entry = touched.get(id);
  if (entry && entry.password !== undefined) row.password = entry.password;   // omitted = keep the saved password
  return row;
}

async function testRow(id, btn, out) {
  btn.disabled = true; out.className = "test-out"; out.textContent = "Testing…";
  try {
    const r = await api("/api/sources/test", rowValues(id));
    out.className = r.ok ? "test-ok" : "test-bad"; out.textContent = (r.ok ? "✔ " : "✖ ") + r.message;
  } catch (e) { out.className = "test-bad"; out.textContent = "✖ " + e.message; }
  btn.disabled = false;
}

async function openSources() {
  srcModal.classList.remove("hidden"); srcMsg("");
  try {
    sources = (await api("/api/sources")).cameras; touched.clear(); buildSourceTable();
    const from = $("#b-from"); from.innerHTML = "";
    for (const c of sources) from.add(new Option(`${c.name} (${c.id})`, c.id));
  } catch (e) { srcMsg(e.message); }
}

function applyBulk() {
  const preset = $("#b-preset").value, main = $("#b-stream").value === "main";
  const start = sources.findIndex(c => c.id === $("#b-from").value);
  const count = Math.max(1, Math.min(64, Number($("#b-count").value) || 1)), first = Number($("#b-ch").value) || 1;
  if (!$("#b-host").value.trim()) return srcMsg("Enter the NVR IP address first.");
  const template = $("#b-custom").value;
  let applied = 0;
  for (let i = 0; i < count && start + i < sources.length; i++) {
    const id = sources[start + i].id, ch = first + i, tr = srcRows.querySelector(`tr[data-id="${id}"]`);
    const set = (key, value) => { const el = $(`[data-key="${key}"]`, tr); el.value = value; markDirty(id, key, value); };
    set("host", $("#b-host").value.trim()); set("port", $("#b-port").value || 554); set("user", $("#b-user").value);
    set("password", $("#b-pass").value);
    set("path", preset === "custom" ? template.replaceAll("{ch}", ch) : PRESETS[preset](ch, main));
    applied++;
  }
  srcMsg(`Filled ${applied} camera${applied === 1 ? "" : "s"}. Press Test on a row, then Save all changes.`, false);
}

async function saveSources() {
  const rows = [...touched.keys()].map(rowValues);
  if (!rows.length) return srcMsg("Nothing changed.", false);
  $("#src-save").disabled = true;
  try {
    const r = await api("/api/sources", {cameras: rows});
    sources = r.cameras; touched.clear(); buildSourceTable();
    srcMsg(`Saved ${r.saved} camera${r.saved === 1 ? "" : "s"}. They reconnect within a few seconds.`, false);
    poll();
  } catch (e) { srcMsg(e.message); }
  $("#src-save").disabled = false;
}

$("#open-sources").onclick = openSources;
$("#src-close").onclick = $("#src-backdrop").onclick = () => srcModal.classList.add("hidden");
$("#b-apply").onclick = applyBulk; $("#src-save").onclick = saveSources;
$("#b-preset").onchange = e => $("#b-custom-wrap").classList.toggle("hidden", e.target.value !== "custom");
document.addEventListener("keydown", e => { if (e.key === "Escape" && !srcModal.classList.contains("hidden")) srcModal.classList.add("hidden"); });

poll(); setInterval(poll, 2000);
setInterval(refreshImages, 1000); setTimeout(refreshImages, 600);
setInterval(manageRtc, 1000);
