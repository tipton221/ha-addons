"""Settings, read from the Home Assistant add-on options file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str
    cf_access_team_domain: str
    cf_access_aud: str
    setup_code: str
    plaid_env: str
    plaid_client_id: str
    plaid_secret: str
    ynab_token: str
    sync_interval_hours: int
    session_hours: int

    @property
    def rp_id(self) -> str:
        """WebAuthn relying party ID: the hostname passkeys are bound to."""
        return urlparse(self.public_url).hostname or "localhost"

    @property
    def origin(self) -> str:
        p = urlparse(self.public_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def cf_access_enabled(self) -> bool:
        return bool(self.cf_access_team_domain and self.cf_access_aud)

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)

    @property
    def oauth_redirect_uri(self) -> str | None:
        # Plaid only accepts HTTPS redirect URIs registered in its dashboard.
        if self.public_url.startswith("https://"):
            return self.public_url.rstrip("/") + "/oauth-return"
        return None


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("FINANCE_DATA_DIR", "/data"))
    options_path = Path(os.environ.get("FINANCE_OPTIONS", data_dir / "options.json"))
    opts: dict = {}
    if options_path.exists():
        opts = json.loads(options_path.read_text())

    def s(key: str, default: str = "") -> str:
        return str(opts.get(key) or default).strip()

    return Settings(
        data_dir=data_dir,
        public_url=s("public_url", "http://localhost:8080").rstrip("/"),
        cf_access_team_domain=s("cf_access_team_domain").removeprefix("https://").rstrip("/"),
        cf_access_aud=s("cf_access_aud"),
        setup_code=s("setup_code"),
        plaid_env=s("plaid_env", "production"),
        plaid_client_id=s("plaid_client_id"),
        plaid_secret=s("plaid_secret"),
        ynab_token=s("ynab_token"),
        sync_interval_hours=int(opts.get("sync_interval_hours") or 4),
        session_hours=int(opts.get("session_hours") or 24),
    )
