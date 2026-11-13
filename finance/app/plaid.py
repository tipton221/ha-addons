"""Minimal Plaid API client (Link tokens, token exchange, accounts, transactions sync)."""

from __future__ import annotations

import httpx

from .config import Settings

HOSTS = {
    "production": "https://production.plaid.com",
    "sandbox": "https://sandbox.plaid.com",
}


class PlaidError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        self.error_code = body.get("error_code", "")
        super().__init__(f"{self.error_code}: {body.get('error_message', '')}")


class Plaid:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=HOSTS[settings.plaid_env], timeout=30)

    async def _post(self, path: str, body: dict) -> dict:
        payload = {
            "client_id": self.settings.plaid_client_id,
            "secret": self.settings.plaid_secret,
            **body,
        }
        r = await self.client.post(path, json=payload)
        data = r.json()
        if r.status_code != 200:
            raise PlaidError(r.status_code, data)
        return data

    async def link_token(self, access_token: str | None = None) -> str:
        body: dict = {
            "client_name": "Finance",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": "owner"},
        }
        if access_token:
            # Update mode: re-authenticate an existing connection.
            body["access_token"] = access_token
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": 730}
        if self.settings.oauth_redirect_uri:
            body["redirect_uri"] = self.settings.oauth_redirect_uri
        return (await self._post("/link/token/create", body))["link_token"]

    async def exchange(self, public_token: str) -> tuple[str, str]:
        data = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return data["access_token"], data["item_id"]

    async def accounts(self, access_token: str) -> list[dict]:
        return (await self._post("/accounts/get", {"access_token": access_token}))["accounts"]

    async def transactions_sync(self, access_token: str, cursor: str | None) -> dict:
        body: dict = {"access_token": access_token, "count": 500}
        if cursor:
            body["cursor"] = cursor
        return await self._post("/transactions/sync", body)

    async def remove_item(self, access_token: str) -> None:
        await self._post("/item/remove", {"access_token": access_token})
