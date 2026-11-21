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

    async def link_token(self, access_token: str | None = None, choose_accounts: bool = False,
                         add_investments: bool = False, investments_only: bool = False) -> str:
        body: dict = {
            "client_name": "Finance",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": "owner"},
        }
        if access_token:
            # Update mode: re-authenticate an existing connection, or (choose_accounts) pick which of
            # its accounts are shared, to add one that was left out the first time.
            body["access_token"] = access_token
            if choose_accounts:
                body["update"] = {"account_selection_enabled": True}
            if add_investments:
                # Consent for Investments on an existing connection (Plaid: "products" must stay out).
                body["additional_consented_products"] = ["investments"]
        elif investments_only:
            # Retirement plan sites that don't offer bank transactions (a 403(b) record keeper).
            body["products"] = ["investments"]
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": 730}
        if self.settings.oauth_redirect_uri:
            body["redirect_uri"] = self.settings.oauth_redirect_uri
        if self.settings.plaid_webhook_url and not access_token:
            body["webhook"] = self.settings.plaid_webhook_url  # a new connection; existing ones: set_webhook
        return (await self._post("/link/token/create", body))["link_token"]

    async def set_webhook(self, access_token: str, url: str) -> None:
        await self._post("/item/webhook/update", {"access_token": access_token, "webhook": url})

    async def webhook_key(self, key_id: str) -> dict:
        """The public key that signs Plaid's webhooks with this key ID."""
        return (await self._post("/webhook_verification_key/get", {"key_id": key_id}))["key"]

    async def exchange(self, public_token: str) -> tuple[str, str]:
        data = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return data["access_token"], data["item_id"]

    async def accounts(self, access_token: str) -> list[dict]:
        return (await self._post("/accounts/get", {"access_token": access_token}))["accounts"]

    async def transactions_sync(self, access_token: str, cursor: str | None) -> dict:
        body: dict = {"access_token": access_token, "count": 500,
                      "options": {"include_original_description": True}}
        if cursor:
            body["cursor"] = cursor
        return await self._post("/transactions/sync", body)

    async def investments_holdings(self, access_token: str) -> dict:
        return await self._post("/investments/holdings/get", {"access_token": access_token})

    async def investments_transactions(self, access_token: str, start: str, end: str) -> dict:
        """Every investment transaction between two dates (Plaid pages them 500 at a time)."""
        txns, securities, offset = [], {}, 0
        while True:
            page = await self._post("/investments/transactions/get", {
                "access_token": access_token, "start_date": start, "end_date": end,
                "options": {"count": 500, "offset": offset}})
            txns += page["investment_transactions"]
            for sec in page.get("securities") or []:
                securities[sec["security_id"]] = sec
            offset = len(txns)
            if offset >= page.get("total_investment_transactions", 0) or not page["investment_transactions"]:
                break
        return {"investment_transactions": txns, "securities": list(securities.values())}

    async def remove_item(self, access_token: str) -> None:
        await self._post("/item/remove", {"access_token": access_token})
