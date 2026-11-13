"""YNAB API client: budgets, accounts, and transactions (delta requests)."""

from __future__ import annotations

import httpx

from .config import Settings

BASE = "https://api.ynab.com/v1"
BUDGET = "last-used"


class YNAB:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=BASE, timeout=60)

    @property
    def configured(self) -> bool:
        return bool(self.settings.ynab_token)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        r = await self.client.get(
            path, params=params, headers={"Authorization": f"Bearer {self.settings.ynab_token}"}
        )
        r.raise_for_status()
        return r.json()["data"]

    async def budgets(self) -> list[dict]:
        return (await self._get("/budgets"))["budgets"]

    async def accounts(self) -> list[dict]:
        return (await self._get(f"/budgets/{BUDGET}/accounts"))["accounts"]

    async def transactions(self, server_knowledge: int | None) -> tuple[list[dict], int]:
        """All transactions, or only those changed since server_knowledge."""
        params = {"last_knowledge_of_server": server_knowledge} if server_knowledge else None
        data = await self._get(f"/budgets/{BUDGET}/transactions", params)
        return data["transactions"], data["server_knowledge"]
