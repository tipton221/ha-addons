#!/bin/bash
# Rebuild and restart the Finance container when the main branch has a newer commit (or always,
# with "force"). A failed build, or a new container that doesn't start, keeps the previous one.
set -uo pipefail

# One run at a time: the timer's run and one started by hand (or by setup.sh) would otherwise
# collide in git and Docker. A second run waits for the first to finish.
exec 9>/run/finance-update.lock
flock 9

BASE=/opt/finance-server
SRC="$BASE/src"
cd "$SRC" || exit 1
rm -f .git/index.lock  # left by a run that was stopped partway; safe while holding the lock above
git fetch -q origin main || { echo "Could not reach GitHub; keeping the current version."; exit 0; }
REV=$(git rev-parse origin/main)

# Keep this script and its helpers current too. Each is written to a temporary file and renamed
# into place, so the copy of this script that is running now isn't changed under it.
for f in update.sh status.sh restore.sh; do
  if git show "origin/main:server/$f" > "$BASE/.$f.new" 2>/dev/null; then
    chmod +x "$BASE/.$f.new" && mv -f "$BASE/.$f.new" "$BASE/$f"
  else
    rm -f "$BASE/.$f.new"
  fi
done
OLD=$(cat "$BASE/built" 2>/dev/null || true)

# Check for updates every minute. setup.sh made the timer; this keeps older servers in step.
TIMER=/etc/systemd/system/finance-update.timer
WANT_TIMER='[Unit]
Description=Check for Finance server updates every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target'
if [ -f "$TIMER" ] && [ "$(cat "$TIMER")" != "$WANT_TIMER" ]; then
  printf '%s\n' "$WANT_TIMER" > "$TIMER"
  systemctl daemon-reload && systemctl restart finance-update.timer
fi

# What the app shows at the bottom of Accounts: the running version, and an update in progress.
version_of() { git show "$1:finance/config.yaml" 2>/dev/null | grep -m1 '^version:' | cut -d'"' -f2; }
report() {  # report STATE RUNNING_REV [TARGET_REV]
  STATE="$1" RUN_REV="${2:-}" RUN_VER="$(version_of "${2:-HEAD}")" TARGET_REV="${3:-}" \
  TARGET_VER="$( [ -n "${3:-}" ] && version_of "$3")" AT="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
  OUT="$BASE/data/update.json" python3 - <<'PY'
import json, os
e = os.environ
info = {"state": e["STATE"], "version": e["RUN_VER"], "rev": e["RUN_REV"][:7], "at": e["AT"]}
if e["TARGET_REV"]:
    info |= {"target_version": e["TARGET_VER"], "target_rev": e["TARGET_REV"][:7]}
tmp = e["OUT"] + ".tmp"
with open(tmp, "w") as f:
    json.dump(info, f)
os.replace(tmp, e["OUT"])
PY
}

if [ "${1:-}" != force ]; then
  if [ "$REV" = "$OLD" ] && docker inspect finance >/dev/null 2>&1; then exit 0; fi
  # A version that already failed isn't retried every minute; the next commit (or "force") is.
  if [ "$REV" = "$(cat "$BASE/failed" 2>/dev/null)" ]; then exit 0; fi
fi

report updating "$OLD" "$REV"
git checkout -q --detach "$REV"
if ! docker build -q -t "finance:$REV" finance >/dev/null; then
  echo "Build of $REV failed; keeping the current version."
  echo "$REV" > "$BASE/failed"
  report failed "$OLD" "$REV"
  [ -n "$OLD" ] && git checkout -q --detach "$OLD"
  exit 1
fi

start() {
  docker rm -f finance >/dev/null 2>&1 || true
  docker run -d --name finance --restart unless-stopped -v "$BASE/data:/data" "finance:$1" >/dev/null
}
start "$REV"
# The new version counts as started once the app logs that startup finished.
for _ in $(seq 1 30); do
  docker logs finance 2>&1 | grep "Application startup complete" >/dev/null && break
  sleep 2
done
if ! docker logs finance 2>&1 | grep "Application startup complete" >/dev/null; then
  echo "$REV" > "$BASE/failed"
  report failed "$OLD" "$REV"
  echo "Version $REV didn't start. Last log lines:"
  docker logs --tail 20 finance 2>&1
  if [ -n "$OLD" ] && docker image inspect "finance:$OLD" >/dev/null 2>&1; then
    echo "Going back to $OLD."
    start "$OLD"
    git checkout -q --detach "$OLD"
  fi
  exit 1
fi
echo "$REV" > "$BASE/built"
report current "$REV"
# Keep this version and the one before it; remove older images.
docker images --format '{{.Repository}}:{{.Tag}}' finance | grep -v -e ":$REV$" -e ":${OLD:-none}$" | xargs -r docker rmi -f >/dev/null 2>&1 || true
echo "Running $REV."
