"""YNAB API client. For now only a connection check; transaction import comes next."""

from __future__ import annotations

import httpx

from .config import Settings

BASE = "https://api.ynab.com/v1"


class YNAB:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=BASE, timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self.settings.ynab_token)

    async def budgets(self) -> list[dict]:
        r = await self.client.get(
            "/budgets", headers={"Authorization": f"Bearer {self.settings.ynab_token}"}
        )
        r.raise_for_status()
        return r.json()["data"]["budgets"]
