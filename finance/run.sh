#!/bin/sh
set -e

TOKEN=$(python -c 'import json,os
p=os.environ.get("FINANCE_OPTIONS","/data/options.json")
try:
    print(json.load(open(p)).get("cloudflare_tunnel_token",""))
except FileNotFoundError:
    print("")')

if [ -n "$TOKEN" ]; then
  # Keep the tunnel up if cloudflared exits. The token is passed by
  # environment variable so it does not show in the process list.
  (
    while true; do
      TUNNEL_TOKEN="$TOKEN" cloudflared tunnel --no-autoupdate run || true
      echo "cloudflared exited; restarting in 10 seconds"
      sleep 10
    done
  ) &
else
  echo "No cloudflare_tunnel_token set; the app is not reachable from outside the container."
fi

cd /opt/finance
exec uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 --no-server-header
