"use strict";
// WebRTC (WHEP) playback of a MediaMTX path, signalled through this app's /webrtc proxy (so the login still protects it).
// The browser decodes the camera's own H.264: nothing is re-encoded on the server, so the picture is as sharp as the camera
// sends and as smooth as the network allows. onEvent("playing") once video shows, onEvent("failed", reason) if it never
// starts, drops or freezes (the caller then falls back). Returns stop().
function playWhep(video, path, onEvent) {
  const pc = new RTCPeerConnection({iceServers: []});   // LAN / VPN: the server advertises its own address (webrtcAdditionalHosts)
  let dead = false, playing = false, session = null, lastTime = -1, still = 0, droppedTimer = null;
  const timers = [setTimeout(() => !playing && fail("no video within 8 s"), 8000)];

  function stop() {
    if (dead) return;
    dead = true;
    timers.forEach(clearTimeout); clearInterval(watchdog); clearTimeout(droppedTimer);
    try { pc.close(); } catch (e) { /* already closed */ }
    video.onplaying = null; video.srcObject = null;
    if (session) fetch(session, {method: "DELETE"}).catch(() => {});
  }
  function fail(why) { if (!dead) { stop(); onEvent("failed", why); } }

  pc.addTransceiver("video", {direction: "recvonly"});
  pc.ontrack = e => { video.srcObject = e.streams[0] || new MediaStream([e.track]); video.play().catch(() => {}); };
  video.onplaying = () => { if (!playing && !dead) { playing = true; onEvent("playing"); } };
  pc.onconnectionstatechange = () => {
    clearTimeout(droppedTimer);
    if (pc.connectionState === "failed" || pc.connectionState === "closed") fail("connection " + pc.connectionState);
    else if (pc.connectionState === "disconnected") droppedTimer = setTimeout(() => fail("connection lost"), 3000);
  };
  // A frozen picture that still says "connected": the clock must advance while playing (as the CCTV_MONITOR watchdog).
  const watchdog = setInterval(() => {
    if (!playing || document.hidden) return;
    still = video.currentTime === lastTime ? still + 1 : 0;
    lastTime = video.currentTime;
    if (still >= 3) fail("picture froze");
  }, 2000);

  (async () => {
    await pc.setLocalDescription(await pc.createOffer());
    if (pc.iceGatheringState !== "complete") {
      await new Promise(resolve => {
        const t = setTimeout(resolve, 1500);
        pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") { clearTimeout(t); resolve(); } };
      });
    }
    if (dead) return;
    const res = await fetch(`/webrtc/${encodeURIComponent(path)}/whep`, {method: "POST", headers: {"Content-Type": "application/sdp"}, body: pc.localDescription.sdp});
    if (!res.ok) throw new Error("server said " + res.status);
    session = res.headers.get("Location");
    const answer = await res.text();
    if (dead) { if (session) fetch(session, {method: "DELETE"}).catch(() => {}); return; }
    await pc.setRemoteDescription({type: "answer", sdp: answer});
  })().catch(e => fail(String(e.message || e)));
  return stop;
}
