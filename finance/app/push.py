"""Web Push to the installed web app (Home Screen on iPhone, Dock on Mac): a notification for new
transactions to review or a bank connection that needs attention, with the app icon badge.

Safari requires every push to show a notification (a silent, badge-only push would lose the
permission), so notifications are only sent when something new happens.

Implements the standard directly (RFC 8291 message encryption, RFC 8292 VAPID) with the
cryptography and PyJWT packages the app already uses, so no extra dependency is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import Database

log = logging.getLogger("finance.push")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _hkdf_one(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 for outputs of one block (32 bytes or less)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """aes128gcm content coding for one Web Push message (RFC 8291)."""
    ua_public = unb64u(p256dh)
    as_private = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf_one(unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf_one(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_one(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_public)]) + as_public + body


def decrypt(message: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: str) -> bytes:
    """The reverse of encrypt, as a browser does it (used by the tests)."""
    salt, idlen = message[:16], message[20]
    as_public = message[21:21 + idlen]
    ua_public = _point(ua_private.public_key())
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hkdf_one(unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf_one(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_one(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, message[21 + idlen:], None).rstrip(b"\x00")[:-1]


def vapid_keys(db: Database) -> tuple[ec.EllipticCurvePrivateKey, str]:
    """The app's own signing key for push (made once, kept in the database), and its public key
    as the browser needs it."""
    pem = db.get_kv("vapid_private_pem")
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        db.set_kv("vapid_private_pem", key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    return key, b64u(_point(key.public_key()))


def vapid_header(key: ec.EllipticCurvePrivateKey, public: str, endpoint: str, subject: str) -> str:
    u = urlparse(endpoint)
    token = jwt.encode({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject},
                       key, algorithm="ES256")
    return f"vapid t={token}, k={public}"


async def send(db: Database, message: dict, subject: str, client: httpx.AsyncClient | None = None,
               skip: str = "") -> int:
    """Send to every subscribed device (but skip). Returns how many accepted it; gone subscriptions are dropped."""
    with db.connect() as conn:
        subs = [dict(r) for r in conn.execute("SELECT endpoint, p256dh, auth FROM push_subs WHERE endpoint != ?", (skip,))]
    if not subs:
        return 0
    key, public = vapid_keys(db)
    payload = json.dumps(message).encode()
    sent = 0
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        for s in subs:
            try:
                r = await client.post(s["endpoint"], content=encrypt(payload, s["p256dh"], s["auth"]), headers={
                    "Authorization": vapid_header(key, public, s["endpoint"], subject),
                    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                    "TTL": "86400", "Urgency": "normal"})
            except httpx.HTTPError as e:
                log.warning("Push failed: %s", e)
                continue
            if r.status_code in (404, 410):  # the device unsubscribed or the app was removed
                with db.connect() as conn:
                    conn.execute("DELETE FROM push_subs WHERE endpoint=?", (s["endpoint"],))
            elif r.status_code >= 300:
                log.warning("Push service answered %s: %s", r.status_code, r.text[:200])
            else:
                sent += 1
    finally:
        if own:
            await client.aclose()
    return sent


def money(v: float) -> str:
    return f"${abs(v):,.2f}"


def after_sync_message(db: Database, rows: list[dict], issues: list[dict], badge: int) -> dict | None:
    """What to send after a sync: new transactions to review (each told once; a posted charge whose
    pending version was told isn't told again) and connections that newly need attention."""
    with db.connect() as conn:
        told = {r["id"] for r in conn.execute("SELECT id FROM push_told")}
        broken = set(json.loads(db.get_kv("push_broken") or "[]"))
    new = [t for t in rows if t["id"] not in told and not (t.get("pending_id") and t["pending_id"] in told)]
    now_broken = {i["item_id"] for i in issues}
    fresh_issues = [i for i in issues if i["item_id"] not in broken]
    with db.connect() as conn:
        for t in new:
            conn.execute("INSERT OR IGNORE INTO push_told VALUES (?)", (t["id"],))
    db.set_kv("push_broken", json.dumps(sorted(now_broken)))
    if not new and not fresh_issues:
        return None
    if fresh_issues:
        names = ", ".join(i["institution"] or "A bank" for i in fresh_issues)
        title, body = "Connection needs attention", f"{names}: open Accounts to reconnect."
        if new:
            body += f" Also {len(new)} new transaction{'s' if len(new) > 1 else ''} to review."
        url = "/accounts"
    elif len(new) == 1:
        t = new[0]
        title = t["display_name"]
        body = f"{'+' if t['amount'] < 0 else ''}{money(t['amount'])} · {t['account_label']}" + (" · pending" if t["pending"] else "")
        url = "/"
    else:
        title = f"{len(new)} new transactions"
        shown = new[:4]
        body = ", ".join(f"{t['display_name']} {'+' if t['amount'] < 0 else ''}{money(t['amount'])}" for t in shown)
        if len(new) > len(shown):
            body += f", and {len(new) - len(shown)} more"
        url = "/"
    return {"title": title, "body": body, "badge": badge, "url": url}
