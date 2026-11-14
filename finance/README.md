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

Version 1.3.2:

- Dark, Apple-style design: glass cards, rounded corners, a floating tab bar on phones, narrowed SF Pro headings on Apple devices, and animations for syncing and reviewing.
- Inbox: a collapsed Scheduled list of every upcoming bill (next due at the bottom) that follows the account filter, unreviewed transactions on top with one-tap review and mark all, then every transaction grouped by day, with paging and an account filter (all accounts, or one at a time).
- Every edit or add (transactions, bills, paydays, categories, account names) opens in the same pop-up sheet. Amount fields fill in from the cents as you type (1500 becomes 15.00).
- Tap a transaction to rename the merchant, change its category (for every transaction from that merchant, or just this one), and add a note. Merchant renames and categories apply to past and future transactions.
- Your own categories (a starter list mapped from Plaid's categories, then fully editable, with named or custom hex colors), renamable accounts, and colored recurring tags for transactions that match a bill.
- When a pending charge posts, its review, note, and one-off category carry over; a changed final amount shows as a tip (restaurants) or a difference, and a jump of more than 30% goes back to the inbox.
- Card payments and transfers between your accounts are paired (same amount, opposite sides, within 5 days, at least one side checking): both show a ⇄ tag naming the other account, reviewing one reviews both, and search totals leave them out.
- YNAB names: posted transactions on linked accounts take the payee and memo of the matching YNAB transaction (runs once after upgrading; Compare has Apply again). Merchants with two or more matches on one payee are renamed for the future; others get one-off names. Your own renames and notes win.
- Transaction editor: one choice ("All from this merchant" or "Just this one") covers the name and category, an option to include similar merchants (same first word, for bank descriptions with order numbers), name suggestions while typing, and a Bill or income picker (automatic, none, a specific one, or a new one filled in from the transaction). Saving updates the list in place without losing your scroll position.
- Recurring income (paychecks every 2 weeks, interest) next to bills, with averages for varying amounts. Income shows in Scheduled and never changes coverage. Mark one as your paycheck and its dates become your paydays for coverage (the manual payday setting is the fallback).
- A "Pending only" toggle on the home list shows every pending transaction, reviewed or not, for the chosen account.
- Home screen and browser icons, and a web app manifest.
- Bills with detection, editing, tag colors (named or custom hex), varying amounts shown as an average (≈), the card each bill is paid with (the card of its last charge unless you pick one), and coverage through the next payday; search with CSV export; Plaid versus YNAB comparison.
