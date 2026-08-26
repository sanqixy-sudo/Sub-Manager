#!/bin/sh
set -eu

mkdir -p /data/cache /data/upstreams

cd /base
# A subscription group may contain several independent upstreams. Do not let one
# temporarily broken/unparseable source make the whole group fail.
if [ -f /base/pref.ini ]; then
  sed -i 's/^skip_failed_links=.*/skip_failed_links=true/' /base/pref.ini || true
  sed -i 's/^log_level=.*/log_level=info/' /base/pref.ini || true
fi
subconverter >/data/subconverter.log 2>&1 &
SUB_PID=$!

cleanup() {
  kill "$SUB_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Give subconverter a moment to bind 25500. The API still starts even if it fails;
# /api/health will expose the state and cached subscriptions remain readable.
sleep 0.4

cd /app
exec uvicorn app.main:app --host 0.0.0.0 --port 7777 --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
