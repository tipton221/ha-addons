"""Apple Push Notification service (APNs) for the native Finance app, next to Web Push (push.py).

The app registers its device token with the server (/api/apns/register). Notifications go to
Apple's HTTP/2 API, signed with a token made from the developer account's .p8 key, which lives
in the data folder as apns.p8 (never in options or the repository). The Team ID and bundle ID
come from the apple_app_id option ("TEAMID.com.example.app"), the same value the site's
apple-app-site-association file uses.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import jwt

from .db import Database

log = logging.getLogger("finance.apns")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_SECONDS = 40 * 60    # Apple accepts a token for an hour and wants it reused, not made per push
GONE = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


class APNs:
    def __init__(self, key_path: Path, key_id: str, app_id: str):
        self.key_path = key_path
        self.key_id = key_id.strip()
        team, _, bundle = app_id.strip().partition(".")
        self.team_id, self.bundle_id = team, bundle
        self._token: tuple[float, str] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.team_id and self.bundle_id and self.key_path.exists())

    def auth_token(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if self._token and now - self._token[0] < TOKEN_SECONDS:
            return self._token[1]
        token = jwt.encode({"iss": self.team_id, "iat": int(now)}, self.key_path.read_text(),
                           algorithm="ES256", headers={"kid": self.key_id})
        self._token = (now, token)
        return token

    @staticmethod
    def payload(message: dict) -> dict:
        """The same message Web Push gets, in Apple's format. One collapse ID (set in the headers)
        makes a newer notification replace the last, like the web app's single tag."""
        quiet = bool(message.get("quiet"))
        aps: dict = {
            "alert": {"title": message.get("title") or "Finance", "body": message.get("body") or ""},
            "badge": int(message.get("badge") or 0),
            "thread-id": "finance",
            "interruption-level": "passive" if quiet else "active",
        }
        if not quiet:
            aps["sound"] = "default"
        return {"aps": aps, "url": message.get("url") or "/"}

    async def send(self, db: Database, message: dict, skip: str = "",
                   client: httpx.AsyncClient | None = None) -> int:
        """A notification to every registered device (but skip). One collapse ID makes a newer one
        replace the last. Returns how many Apple accepted."""
        quiet = bool(message.get("quiet"))
        return await self._post(db, self.payload(message), "alert", "5" if quiet else "10", "finance", skip, client)

    async def badge(self, db: Database, count: int, client: httpx.AsyncClient | None = None) -> int:
        """Set the icon badge on every device, with nothing shown. The system sets it even while
        the app isn't running."""
        return await self._post(db, {"aps": {"badge": max(0, int(count))}}, "alert", "5", None, "", client)

    async def refresh(self, db: Database, info: dict, skip: str = "",
                      client: httpx.AsyncClient | None = None) -> int:
        """A silent push the app handles in the background: {"badge": n} and {"clear": true} (remove
        the Finance notifications shown). Apple delivers these as it sees fit (a few an hour, not to
        an app that was force-quit), so the app also catches up whenever it opens."""
        return await self._post(db, {"aps": {"content-available": 1}, "finance": info}, "background", "5", None,
                                skip, client)

    async def _post(self, db: Database, payload: dict, push_type: str, priority: str, collapse: str | None,
                    skip: str, client: httpx.AsyncClient | None) -> int:
        """Send to every registered device (but skip). Returns how many Apple accepted; devices Apple
        says are gone are dropped."""
        if not self.configured:
            return 0
        with db.connect() as conn:
            devices = [dict(r) for r in conn.execute("SELECT token, env FROM apns_devices WHERE token != ?", (skip,))]
        if not devices:
            return 0
        body = json.dumps(payload).encode()
        headers = {"apns-topic": self.bundle_id, "apns-push-type": push_type, "apns-priority": priority,
                   "apns-expiration": str(int(time.time()) + 86400)}
        if collapse:
            headers["apns-collapse-id"] = collapse
        sent = 0
        own = client is None
        client = client or httpx.AsyncClient(http2=True, timeout=15)
        try:
            for d in devices:
                host = HOSTS.get(d["env"], HOSTS["production"])
                try:
                    r = await client.post(f"{host}/3/device/{d['token']}", content=body,
                                          headers={"authorization": f"bearer {self.auth_token()}", **headers})
                except httpx.HTTPError as e:
                    log.warning("APNs request failed: %s", e)
                    continue
                if r.status_code == 200:
                    sent += 1
                    continue
                try:
                    reason = r.json().get("reason", "")
                except ValueError:
                    reason = ""
                if r.status_code == 410 or reason in GONE:
                    with db.connect() as conn:
                        conn.execute("DELETE FROM apns_devices WHERE token=?", (d["token"],))
                else:
                    log.warning("APNs answered %s: %s", r.status_code, reason or r.text[:200])
        finally:
            if own:
                await client.aclose()
        return sent
