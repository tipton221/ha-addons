"""Two login layers.

1. Cloudflare Access: every request must carry a valid Access JWT for this
   application (checked here too, in case the Cloudflare rule is ever changed).
2. Passkeys: an app session cookie issued after a WebAuthn sign-in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import Settings
from .db import Database

SESSION_COOKIE = "finance_session"
CHALLENGE_COOKIE = "finance_challenge"
CHALLENGE_TTL = 300


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CloudflareAccess:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._jwks = None

    def verify(self, token: str | None) -> bool:
        if not self.settings.cf_access_enabled:
            return True
        if not token:
            return False
        team = self.settings.cf_access_team_domain
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True)
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.cf_access_aud,
                issuer=f"https://{team}",
            )
        except Exception:
            return False
        return True


class Sessions:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        expires = _now() + timedelta(hours=self.settings.session_hours)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
            conn.execute(
                "INSERT INTO sessions (token_hash, expires_at) VALUES (?, ?)",
                (_hash(token), expires.isoformat()),
            )
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
        return bool(row) and row["expires_at"] > _now().isoformat()

    def revoke(self, token: str | None) -> None:
        if token:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


class Challenges:
    """Short-lived WebAuthn challenges, kept in memory (single process)."""

    def __init__(self):
        self._store: dict[str, tuple[bytes, float]] = {}

    def issue(self, challenge: bytes) -> str:
        now = time.time()
        self._store = {k: v for k, v in self._store.items() if v[1] > now}
        key = secrets.token_urlsafe(16)
        self._store[key] = (challenge, now + CHALLENGE_TTL)
        return key

    def take(self, key: str | None) -> bytes | None:
        if not key:
            return None
        item = self._store.pop(key, None)
        if not item or item[1] < time.time():
            return None
        return item[0]


class Passkeys:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def count(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM passkeys").fetchone()["n"]

    def _user_id(self) -> bytes:
        uid = self.db.get_kv("webauthn_user_id")
        if uid is None:
            uid = secrets.token_hex(16)
            self.db.set_kv("webauthn_user_id", uid)
        return bytes.fromhex(uid)

    def check_setup_code(self, code: str) -> bool:
        expected = self.settings.setup_code
        if len(expected) < 12:
            return False
        return hmac.compare_digest(code.encode(), expected.encode())

    def registration_options(self) -> tuple[str, bytes]:
        with self.db.connect() as conn:
            existing = [r["credential_id"] for r in conn.execute("SELECT credential_id FROM passkeys")]
        opts = generate_registration_options(
            rp_id=self.settings.rp_id,
            rp_name="Finance",
            user_name="finance",
            user_display_name="Finance",
            user_id=self._user_id(),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in existing],
        )
        return options_to_json(opts), opts.challenge

    def register(self, credential: dict, challenge: bytes, label: str) -> None:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.settings.rp_id,
            expected_origin=self.settings.origin,
            require_user_verification=True,
        )
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO passkeys (credential_id, public_key, sign_count, label) VALUES (?, ?, ?, ?)",
                (verified.credential_id, verified.credential_public_key, verified.sign_count, label[:60]),
            )

    def authentication_options(self) -> tuple[str, bytes]:
        opts = generate_authentication_options(
            rp_id=self.settings.rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json(opts), opts.challenge

    def authenticate(self, credential: dict, challenge: bytes) -> bool:
        from webauthn import base64url_to_bytes

        raw_id = base64url_to_bytes(credential.get("rawId") or credential.get("id", ""))
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT public_key, sign_count FROM passkeys WHERE credential_id = ?", (raw_id,)
            ).fetchone()
        if not row:
            return False
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.settings.rp_id,
            expected_origin=self.settings.origin,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE passkeys SET sign_count = ? WHERE credential_id = ?",
                (verified.new_sign_count, raw_id),
            )
        return True
