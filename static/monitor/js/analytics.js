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
    <div class="tile__body"><img class="tile__img" alt=""><div class="tile__placeholder"></div></div>
    <div class="tile__metrics">
      <div class="metric"><small></small><strong></strong></div><div class="metric"><small></small><strong></strong></div>
      <div class="metric"><small></small><strong></strong></div><div class="metric"><small></small><strong></strong></div></div>`;
  el.tabIndex = 0; el.setAttribute("role", "button"); el.setAttribute("aria-label", "Open " + c.name);
  el.addEventListener("click", () => openModal(c.id));
  el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openModal(c.id); } });
  observer.observe(el);
  grid.appendChild(el);
  const t = {el, img: $(".tile__img", el), loading: false};
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
  $("#infer").textContent = `Gate tracking ${totals.infer_per_s}/s · Crowd model: ${totals.crowd_model}`;
  if (openId) updateLive(cameras.find(c => c.id === openId));
}

async function poll() {
  try { data = await api("/api/cameras"); render(); } catch (e) { $("#infer").textContent = "Disconnected"; }
}

// Tiles show a refreshed snapshot (browsers cap ~6 connections/host, so 40 live MJPEG streams would stall).
function refreshImages() {
  for (const [id, t] of tiles) {
    if (t.loading || !inView.has(id) || t.el.dataset.status !== "live" || t.el.classList.contains("hidden")) continue;
    t.loading = true;
    const probe = new Image();
    probe.onload = () => { t.img.src = probe.src; t.loading = false; };
    probe.onerror = () => { t.loading = false; };
    probe.src = `/api/cameras/${id}/frame.jpg?w=480&t=${Date.now()}`;
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
  const cells = $("#p-live").children;
  const slow = tracks(c) && c.status === "live" && c.fps < 0.8 * c.target_fps;
  const parts = [];
  if (tracks(c)) parts.push(`Tracking ${c.fps}/s (target ${c.target_fps}/s)` + (slow ? " – too slow, crossings may be missed" : ""));
  if (dense(c)) parts.push(c.counted_ago == null ? "Crowd count pending" : `Crowd count ${c.counted_ago} s ago`);
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
  showRole(cfg.role); updateLive(d);
}

async function openModal(id) {
  openId = id;
  $("#m-panel").innerHTML = panelHTML;
  $("#modal").classList.remove("hidden");
  $("#m-live").src = `/api/cameras/${id}/stream.mjpg`;
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
  endDraw(); openId = null;
  $("#m-live").src = "data:,";   // drops the MJPEG connection
  $("#modal").classList.add("hidden");
}
$("#m-close").onclick = closeModal; $("#m-backdrop").onclick = closeModal;
document.addEventListener("keydown", e => { if (e.key === "Escape" && openId) closeModal(); });

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
  $("#m-live").style.display = "block"; stage.style.display = "none"; overlay.innerHTML = ""; overlay.classList.remove("edit");
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
  $("#m-live").style.display = "none"; stage.style.display = "block"; $("#b-cancel").hidden = false;
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

poll(); setInterval(poll, 2000);
setInterval(refreshImages, 3000); setTimeout(refreshImages, 600);
