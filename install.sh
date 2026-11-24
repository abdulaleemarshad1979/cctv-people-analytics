#!/usr/bin/env bash
# One-command install / update for Ubuntu:
#   curl -fsSL https://raw.githubusercontent.com/abdulaleemarshad1979/cctv-people-analytics/main/install.sh | sudo bash
# Installs Docker (if missing), fetches the code and model weights, and starts the app + MediaMTX.
# Re-run it any time to update. Env: INSTALL_DIR, REPO, WEIGHTS_TAG, SKIP_DEPS=1 (skip apt/docker, no root needed).
set -euo pipefail

REPO="${REPO:-abdulaleemarshad1979/cctv-people-analytics}"
DIR="${INSTALL_DIR:-/opt/cctv-people-analytics}"
WEIGHTS_TAG="${WEIGHTS_TAG:-weights-v1}"
say() { printf '\n==> %s\n' "$*"; }

if [ "${SKIP_DEPS:-0}" != 1 ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Run as root: curl -fsSL <url> | sudo bash" >&2; exit 1; }
  say "Installing git, curl and Docker"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends git curl ca-certificates
  if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y docker.io docker-compose-v2 || { curl -fsSL https://get.docker.com | sh; }
    systemctl enable --now docker
  fi
fi

say "Fetching code into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "https://github.com/$REPO.git" "$DIR"
fi
cd "$DIR"

say "Fetching model weights (DM-Count, CSRNet)"
mkdir -p weights config
while read -r sum name; do
  if [ ! -f "weights/$name" ] || ! (cd weights && echo "$sum  $name" | sha256sum -c --quiet -); then
    curl -fL --retry 3 -o "weights/$name.part" "https://github.com/$REPO/releases/download/$WEIGHTS_TAG/$name"
    mv "weights/$name.part" "weights/$name"
  fi
done < weights.sha256
(cd weights && sha256sum -c --quiet ../weights.sha256)

say "Writing configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi
# An untouched earlier copy (no camera paths in it) is replaced so it gets the API and WebRTC settings.
{ [ -f config/mediamtx.yml ] && grep -q 'source:' config/mediamtx.yml; } || cp mediamtx/mediamtx.example.yml config/mediamtx.yml
ips="$(hostname -I 2>/dev/null | awk '{$1=$1; gsub(" ", ","); print}')"
if [ -n "$ips" ]; then   # browsers need this server's address for live video (an older .env has no such line)
  if ! grep -q '^WEBRTC_HOSTS=' .env; then
    printf '\nWEBRTC_HOSTS=127.0.0.1,%s\n' "$ips" >> .env
  elif grep -q '^WEBRTC_HOSTS=127.0.0.1$' .env; then
    sed -i "s/^WEBRTC_HOSTS=.*/WEBRTC_HOSTS=127.0.0.1,$ips/" .env
  fi
fi
[ -f config/nvrs.json ] || cp nvrs.example.json config/nvrs.json
chmod 600 config/nvrs.json
chown -R 1000:1000 config 2>/dev/null || true   # the app container runs as uid 1000

say "Building and starting (first build downloads PyTorch; this takes a few minutes)"
docker compose up -d --build
docker compose restart gateway   # nginx.conf is mounted: a changed file is only read on restart

port="$(sed -n 's/^WEB_PORT=\([0-9]*\).*/\1/p' .env | head -1)"
host="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done"
echo "Dashboard: http://${host:-<server-ip>}:${port:-5000}"
echo "Login:     $(sed -n 's/^LOGIN_USER=\([^ #]*\).*/\1/p' .env) / $(sed -n "s/^LOGIN_PASSWORD=\(.*\)/\1/p" .env | tr -d "'")   (change it: sudo $DIR/scripts/set-password.sh)"
echo
echo "Next: put your NVR details in $DIR/config/nvrs.json, then run:"
echo "  cd $DIR && python3 scripts/gen_mediamtx.py && docker compose restart mediamtx"
