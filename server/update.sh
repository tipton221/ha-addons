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
OLD=$(cat "$BASE/built" 2>/dev/null || true)
if [ "${1:-}" != force ]; then
  if [ "$REV" = "$OLD" ] && docker inspect finance >/dev/null 2>&1; then exit 0; fi
  # A version that already failed isn't retried every 15 minutes; the next commit (or "force") is.
  if [ "$REV" = "$(cat "$BASE/failed" 2>/dev/null)" ]; then exit 0; fi
fi

git checkout -q --detach "$REV"
if ! docker build -q -t "finance:$REV" finance >/dev/null; then
  echo "Build of $REV failed; keeping the current version."
  echo "$REV" > "$BASE/failed"
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
  docker logs finance 2>&1 | grep -q "Application startup complete" && break
  sleep 2
done
if ! docker logs finance 2>&1 | grep -q "Application startup complete"; then
  echo "$REV" > "$BASE/failed"
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
# Keep this version and the one before it; remove older images.
docker images --format '{{.Repository}}:{{.Tag}}' finance | grep -v -e ":$REV$" -e ":${OLD:-none}$" | xargs -r docker rmi -f >/dev/null 2>&1 || true
echo "Running $REV."
