#!/bin/bash
# A short status of the Finance server, with no secrets in it.
BASE=/opt/finance-server
echo "==== Status (safe to share: no secrets) ===="
echo "Version: $(grep -m1 '^version:' "$BASE/src/finance/config.yaml" | cut -d'"' -f2) ($(cut -c1-7 "$BASE/built"))"
echo "Container: $(docker inspect -f '{{.State.Status}}' finance 2>/dev/null || echo missing)"
echo "Tunnel connections: $(docker logs finance 2>&1 | grep -c 'Registered tunnel connection' || true)"
echo "App started: $(docker logs finance 2>&1 | grep -q 'Application startup complete' && echo yes || echo no)"
echo "Update timer: $(systemctl is-active finance-update.timer)"
