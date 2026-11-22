#!/bin/bash
# One-time setup of the Finance server on a fresh Ubuntu 24.04 machine (for example a small cloud
# droplet). Run as root:
#   curl -fsSL https://raw.githubusercontent.com/tipton221/ha-addons/main/server/setup.sh -o setup.sh && bash setup.sh
#
# It installs Docker, builds the same container the Home Assistant add-on uses, asks for the app's
# options (secrets are typed, never shown), and starts it. No ports are opened: the container's
# bundled Cloudflare Tunnel is the only way in. A timer then checks every minute for a newer
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
git -C "$BASE/src" checkout -q --detach origin/main
cp "$BASE/src/server/update.sh" "$BASE/src/server/status.sh" "$BASE/src/server/restore.sh" "$BASE/"
chmod +x "$BASE/update.sh" "$BASE/status.sh" "$BASE/restore.sh"

OPTIONS="$BASE/data/options.json"
# Current values, so each question can keep what's already saved (press Enter).
current() { python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except FileNotFoundError: print("")' "$OPTIONS" "$1"; }
ask() {  # ask VAR option_key "Prompt" [secret]
  local cur v shown
  cur=$(current "$2")
  if [ "${4:-}" = secret ]; then
    shown=$([ -n "$cur" ] && echo " [saved; Enter keeps it]" || true)
    read -rsp "$3$shown: " v; echo
  else
    shown=$([ -n "$cur" ] && echo " [$cur]" || true)
    read -rp "$3$shown: " v
  fi
  [ -z "$v" ] && v="$cur"
  [ "$v" = "-" ] && v=""  # a single dash clears a saved value
  printf -v "$1" '%s' "$v"
}
write_options=yes
if [ -f "$OPTIONS" ]; then
  read -rp "Options already saved. Change any of them? [y/N] " a
  [ "${a,,}" = y ] || write_options=no
fi
if [ "$write_options" = yes ]; then
  echo "==> App options (Enter keeps what is saved; a single dash clears it)"
  ask PUBLIC_URL public_url "Web address, e.g. https://finance.example.com"
  ask TUNNEL cloudflare_tunnel_token "Cloudflare Tunnel token" secret
  ask TEAM cf_access_team_domain "Cloudflare Access team domain (optional)"
  ask AUD cf_access_aud "Cloudflare Access application audience tag (optional)"
  while :; do
    ask SETUP setup_code "Setup code for adding the first passkey (at least 12 characters)" secret
    [ ${#SETUP} -ge 12 ] && break
    echo "That's ${#SETUP} characters; the app needs at least 12."
  done
  ask PLAID_ID plaid_client_id "Plaid client ID (optional)"
  ask PLAID_SECRET plaid_secret "Plaid secret (optional)" secret
  ask YNAB ynab_token "YNAB token (optional)" secret
  ask APPLE apple_app_id "Native app ID for passkeys, Team ID then bundle ID, e.g. ABCDE12345.com.example.app (optional)"
  ask APNS_KEY apns_key_id "Apple push Key ID (optional; the key file goes in $BASE/data/apns.p8)"
  PUBLIC_URL="$PUBLIC_URL" TUNNEL="$TUNNEL" TEAM="$TEAM" AUD="$AUD" SETUP="$SETUP" \
  PLAID_ID="$PLAID_ID" PLAID_SECRET="$PLAID_SECRET" YNAB="$YNAB" APPLE="$APPLE" APNS_KEY="$APNS_KEY" OPTIONS="$OPTIONS" python3 - <<'PY'
import json, os
e = os.environ
path = e["OPTIONS"]
try:
    opts = json.load(open(path))  # keep anything saved that isn't asked about here
except FileNotFoundError:
    opts = {}
opts |= {
    "public_url": e["PUBLIC_URL"].strip(), "cloudflare_tunnel_token": e["TUNNEL"].strip(),
    "cf_access_team_domain": e["TEAM"].strip(), "cf_access_aud": e["AUD"].strip(),
    "setup_code": e["SETUP"].strip(), "plaid_env": "production", "plaid_client_id": e["PLAID_ID"].strip(),
    "plaid_secret": e["PLAID_SECRET"].strip(), "ynab_token": e["YNAB"].strip(),
    "apple_app_id": e["APPLE"].strip(), "apns_key_id": e["APNS_KEY"].strip(),
}
opts.setdefault("sync_interval_hours", 1)
opts.pop("session_hours", None)
opts.setdefault("session_days", 30)
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
Description=Check for Finance server updates every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable -q finance-update.timer
systemctl start finance-update.timer



echo
"$BASE/status.sh"
