#!/bin/bash
# A short status of the Finance server, with no secrets in it.
BASE=/opt/finance-server
SINCE=$(docker inspect -f '{{.State.StartedAt}}' finance 2>/dev/null)
logs() { [ -n "$SINCE" ] && docker logs --since "$SINCE" finance 2>&1; }
echo "==== Status (safe to share: no secrets) ===="
echo "Version: $(grep -m1 '^version:' "$BASE/src/finance/config.yaml" | cut -d'"' -f2) ($(cut -c1-7 "$BASE/built" 2>/dev/null))"
echo "Web address: $(python3 -c 'import json;print(json.load(open("/opt/finance-server/data/options.json")).get("public_url",""))' 2>/dev/null)"
echo "Container: $(docker inspect -f '{{.State.Status}}' finance 2>/dev/null || echo missing)"
echo "Tunnel connections since start: $(logs | grep -c 'Registered tunnel connection')"
echo "App started: $(logs | grep -q 'Application startup complete' && echo yes || echo no)"
echo "Update timer: $(systemctl is-active finance-update.timer)"
