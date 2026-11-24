#!/bin/sh
set -e
# First start: seed the editable camera list (kept in the ./config volume afterwards).
[ -f "$ANALYTICS_CONFIG" ] || cp /app/cameras.example.json "$ANALYTICS_CONFIG"
exec gunicorn --worker-class gthread --workers 1 --threads "${WEB_THREADS:-64}" --timeout 0 \
  --bind "0.0.0.0:${WEB_PORT}" --access-logfile /dev/null wsgi:app
