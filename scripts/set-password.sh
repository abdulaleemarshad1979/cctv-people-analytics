#!/usr/bin/env bash
# Set the dashboard login password.
#   sudo scripts/set-password.sh              (asks for the new password, hidden)
#   sudo scripts/set-password.sh 'MyNewPass'  (one command, no prompt)
set -euo pipefail
cd "$(dirname "$0")/.."
pw="${1:-}"
if [ -z "$pw" ]; then
  read -rsp "New password: " pw; echo
  read -rsp "Repeat it:    " again; echo
  [ "$pw" = "$again" ] || { echo "Passwords differ; nothing changed." >&2; exit 1; }
fi
[ "${#pw}" -ge 6 ] || { echo "Use at least 6 characters." >&2; exit 1; }
case "$pw" in *\'*) echo "Single quotes are not allowed in the password." >&2; exit 1;; esac

[ -f .env ] || cp .env.example .env
tmp="$(mktemp)"
grep -v '^LOGIN_PASSWORD=' .env > "$tmp" || true
printf "LOGIN_PASSWORD='%s'\n" "$pw" >> "$tmp"   # single quotes: docker compose reads it literally ($, # are safe)
install -m 600 "$tmp" .env
rm -f "$tmp"
docker compose up -d
echo "Password changed. Everyone is signed out; log in as $(sed -n 's/^LOGIN_USER=\([^ #]*\).*/\1/p' .env) with the new password."
