#!/bin/bash
# Replace the server's database with a full backup downloaded from the app (Accounts > Full backup).
# Run as root: /opt/finance-server/restore.sh /root/finance-backup-YYYY-MM-DD.db
# The current database is kept next to it as finance.db.before-restore-<time>, so this can be undone.
set -euo pipefail

BASE=/opt/finance-server
DATA="$BASE/data"
FILE="${1:-}"
[ "$(id -u)" = 0 ] || { echo "Run this as root."; exit 1; }
[ -f "$FILE" ] || { echo "Usage: $0 /path/to/finance-backup.db"; exit 1; }

echo "==> Checking the backup"
python3 - "$FILE" <<'PY'
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
except sqlite3.DatabaseError as e:
    sys.exit(f"Not a Finance backup ({e}). Nothing was changed.")
missing = {"transactions", "accounts", "passkeys", "plaid_items"} - tables
if ok != "ok" or missing:
    sys.exit(f"Not a usable Finance backup (integrity: {ok}; missing tables: {', '.join(sorted(missing)) or 'none'}). Nothing was changed.")
n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
p = conn.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0]
print(f"OK: {n} transactions, {p} passkey(s).")
PY

echo "==> Swapping it in"
docker stop finance >/dev/null 2>&1 || true
if [ -f "$DATA/finance.db" ]; then
  KEEP="$DATA/finance.db.before-restore-$(date +%Y%m%d-%H%M%S)"
  mv "$DATA/finance.db" "$KEEP"
  echo "Previous database kept as $KEEP"
fi
rm -f "$DATA/finance.db-wal" "$DATA/finance.db-shm"
install -m 600 "$FILE" "$DATA/finance.db"
docker start finance >/dev/null
for _ in $(seq 1 30); do
  docker logs --since "$(docker inspect -f '{{.State.StartedAt}}' finance)" finance 2>&1 | grep "Application startup complete" >/dev/null && break
  sleep 2
done
echo
"$BASE/status.sh"
echo "Delete the backup file once everything works: rm $FILE"
