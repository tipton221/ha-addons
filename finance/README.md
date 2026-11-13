# Finance add-on

Home Assistant add-on for a self-hosted personal finance tracker that pulls transactions from Plaid (and checks a YNAB connection), served at its own HTTPS address through a Cloudflare Tunnel. Design notes and setup decisions are kept privately, outside this repository.

It runs a small web app (FastAPI, SQLite) and a bundled `cloudflared` connector in one container. No ports are published: the app listens on `127.0.0.1:8080` inside the container, and the Cloudflare Tunnel is the only way in. Home Assistant ingress is not used.

## Options

| Option | What to enter |
|---|---|
| `public_url` | The app's address, for example `https://finance.example.com` |
| `cloudflare_tunnel_token` | The tunnel token from Cloudflare One (Networking > Tunnels, then the tunnel), the long string after `--token` |
| `cf_access_team_domain` | `<team name>.cloudflareaccess.com` |
| `cf_access_aud` | The Access application's Application Audience (AUD) tag (Access controls > Applications, then the application) |
| `setup_code` | A random value of at least 12 characters. Needed once, to create the first passkey; clear it afterward |
| `plaid_env` | `production` (the Trial plan uses production) |
| `plaid_client_id`, `plaid_secret` | From the Plaid dashboard, Build > Keys |
| `ynab_token` | YNAB personal access token (YNAB Account Settings > Developer Settings) |
| `sync_interval_hours` | How often to pull from Plaid (default 4) |
| `session_hours` | How long a passkey sign-in lasts (default 24) |

## Login layers

1. Cloudflare Access blocks everyone but the allowed user before traffic reaches the Pi. When `cf_access_team_domain` and `cf_access_aud` are set, the app also verifies the Access token on every request and refuses requests without one.
2. Passkeys (Face ID or Touch ID). The first passkey is created at `/setup` with the setup code. Once a passkey exists, setup closes and more passkeys can only be added while signed in.

State-changing requests also require a custom header that other sites cannot send.

## Data

Everything is stored in `/data/finance.db` inside the add-on: passkey public keys, sessions, Plaid access tokens, accounts, and transactions. `/data` is included in Home Assistant backups, so backups must stay encrypted. Nothing from `/data` goes into git.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
mkdir -p /tmp/fin && echo '{"public_url":"http://localhost:8080","setup_code":"local-dev-setup-code"}' > /tmp/fin/options.json
FINANCE_DATA_DIR=/tmp/fin .venv/bin/uvicorn app.main:create_app --factory --port 8080
```

Passkeys work on `http://localhost` for local testing. Plaid OAuth institutions need the HTTPS address, because Plaid only redirects to registered HTTPS URIs.

## Status

Version 0.3.0:

- Inbox home page: unreviewed transactions (pending included), one-tap review, mark all reviewed. History older than a week arrives already reviewed.
- Bills: recurring charges detected from Plaid history and editable (amount, cadence, day, match text, next due date). Bills that stop charging are flagged and left out.
- Coverage: cash minus card balances, pending card charges, and bills due before the next payday (twice-monthly days or every two weeks).
- Search by merchant, dates, amounts, account, and category, with CSV export of the results.
- Accounts page with Plaid connections, YNAB import status, and the Plaid versus YNAB comparison.
