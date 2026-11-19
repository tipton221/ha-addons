#!/bin/bash
# One-time setup of the Finance server on a fresh Ubuntu 24.04 machine (for example a small cloud
# droplet). Run as root:
#   curl -fsSL https://raw.githubusercontent.com/tipton221/ha-addons/main/server/setup.sh -o setup.sh && bash setup.sh
#
# It installs Docker, builds the same container the Home Assistant add-on uses, asks for the app's
# options (secrets are typed, never shown), and starts it. No ports are opened: the container's
# bundled Cloudflare Tunnel is the only way in. A timer then checks every 15 minutes for a newer
# version on the main branch and rebuilds; a failed build or start keeps the running version.
# Running it again keeps the existing data and asks whether to replace the options.
set -euo pipefail

BASE=/opt/finance-server
REPO=https://github.com/tipton221/ha-addons.git

[ "$(id -u)" = 0 ] || { echo "Run this as root (sudo bash setup.sh)."; exit 1; }

echo "==> Installing Docker and git"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null || { apt-get update -q && apt-get install -yq git; }

echo "==> Getting the code"
mkdir -p "$BASE/data"
chmod 700 "$BASE/data"
if [ -d "$BASE/src/.git" ]; then
  git -C "$BASE/src" fetch -q origin main
else
  git clone -q "$REPO" "$BASE/src"
fi
cp "$BASE/src/server/update.sh" "$BASE/src/server/status.sh" "$BASE/"
chmod +x "$BASE/update.sh" "$BASE/status.sh"

OPTIONS="$BASE/data/options.json"
ask() {  # ask NAME "Prompt" [secret]
  local v
  if [ "${3:-}" = secret ]; then read -rsp "$2: " v; echo; else read -rp "$2: " v; fi
  printf -v "$1" '%s' "$v"
}
write_options=yes
if [ -f "$OPTIONS" ]; then
  read -rp "Options already saved. Replace them? [y/N] " a
  [ "${a,,}" = y ] || write_options=no
fi
if [ "$write_options" = yes ]; then
  echo "==> App options (press Enter to leave an optional one empty)"
  ask PUBLIC_URL "Web address, e.g. https://finance.example.com"
  ask TUNNEL "Cloudflare Tunnel token" secret
  ask TEAM "Cloudflare Access team domain (optional)"
  ask AUD "Cloudflare Access application audience tag (optional)"
  while :; do
    ask SETUP "Setup code for adding the first passkey (at least 12 characters)" secret
    [ ${#SETUP} -ge 12 ] && break
    echo "That's ${#SETUP} characters; the app needs at least 12."
  done
  ask PLAID_ID "Plaid client ID (optional)"
  ask PLAID_SECRET "Plaid secret (optional)" secret
  ask YNAB "YNAB token (optional)" secret
  PUBLIC_URL="$PUBLIC_URL" TUNNEL="$TUNNEL" TEAM="$TEAM" AUD="$AUD" SETUP="$SETUP" \
  PLAID_ID="$PLAID_ID" PLAID_SECRET="$PLAID_SECRET" YNAB="$YNAB" OPTIONS="$OPTIONS" python3 - <<'PY'
import json, os
e = os.environ
opts = {
    "public_url": e["PUBLIC_URL"].strip(), "cloudflare_tunnel_token": e["TUNNEL"].strip(),
    "cf_access_team_domain": e["TEAM"].strip(), "cf_access_aud": e["AUD"].strip(),
    "setup_code": e["SETUP"].strip(), "plaid_env": "production", "plaid_client_id": e["PLAID_ID"].strip(),
    "plaid_secret": e["PLAID_SECRET"].strip(), "ynab_token": e["YNAB"].strip(),
    "sync_interval_hours": 1, "session_hours": 24,
}
path = e["OPTIONS"]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(opts, f, indent=2)
os.chmod(path, 0o600)  # also when the file already existed
PY
fi

echo "==> Building and starting (the first build takes a few minutes)"
"$BASE/update.sh" force || echo "The build or start failed; see the messages above."

echo "==> Automatic updates"
cat > /etc/systemd/system/finance-update.service <<UNIT
[Unit]
Description=Update the Finance server from the main branch
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$BASE/update.sh
UNIT
cat > /etc/systemd/system/finance-update.timer <<'UNIT'
[Unit]
Description=Check for Finance server updates every 15 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable -q finance-update.timer
systemctl start finance-update.timer



echo
"$BASE/status.sh"
