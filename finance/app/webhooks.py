"""Plaid webhooks: Plaid posts to /plaid/webhook when it has new data for a connection, so the
server syncs right away instead of waiting for its next scheduled sync.

The path can't sit behind the login, so every message is checked before it's acted on. Plaid
signs each one with a JWT in the Plaid-Verification header (ES256, key ID in its header); the
key comes from Plaid's /webhook_verification_key/get, the token must be under five minutes old,
and its request_body_sha256 claim must match the body. Even a valid message only starts a sync.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import jwt
from jwt.algorithms import ECAlgorithm

log = logging.getLogger("finance.webhooks")

MAX_AGE = 5 * 60   # seconds, as Plaid recommends


class Verifier:
    def __init__(self, plaid):
        self.plaid = plaid
        self._keys: dict[str, dict] = {}

    async def _key(self, kid: str) -> dict | None:
        if kid not in self._keys:
            try:
                self._keys[kid] = await self.plaid.webhook_key(kid)
            except Exception as e:  # unknown key ID, Plaid unreachable
                log.warning("Couldn't get Plaid's webhook key %s: %s", kid, e)
                return None
        return self._keys[kid]

    async def verify(self, token: str | None, body: bytes, now: float | None = None) -> bool:
        if not token:
            return False
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return False
        if header.get("alg") != "ES256" or not header.get("kid"):
            return False
        key = await self._key(header["kid"])
        if not key or key.get("expired_at"):
            return False
        try:
            public = ECAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(token, public, algorithms=["ES256"], options={"verify_iat": False})
        except (jwt.PyJWTError, ValueError):
            return False
        now = time.time() if now is None else now
        iat = claims.get("iat")
        if not isinstance(iat, (int, float)) or abs(now - iat) > MAX_AGE:
            return False
        return hmac.compare_digest(str(claims.get("request_body_sha256", "")), hashlib.sha256(body).hexdigest())
