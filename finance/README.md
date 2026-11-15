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

Version 1.11.3:

- Insight tiles: red for worse than usual, green for better, gray for neutral. More room between the numbers at the top of Net worth and Spending.
- Cash flow: Kept is now Net, with its share of income on a second line on every row so rows are the same height. Saved also counts transfers Plaid marks as going to (or coming from) a retirement or investment account, such as a Roth IRA contribution, even when that account isn't connected.

- Insights tiles say what their color means in a badge (Above usual, Below usual, +$X vs usual, Kept N%), use plain labels ("November spending so far", with where the month is heading), and stack one per row on phones.
- The spending donut is larger and interactive: hover or tap a slice, or hover a category in the list, to see its name, amount, and share in the middle; click (or tap again) to open those transactions in Search.

- Insights remembers where you were (scroll, month, range, open lists) when you follow a link or save and then come back; a plain tab visit starts at the top. Spending shows the three largest purchases with "See all" for every spending transaction in the month, largest first, as editable rows. Larger spending chart, a compact month stepper, and any account can be left out of net worth (with its history) or included again.
- Money fields add thousands commas as you type (1,234.50), and the app accepts amounts with commas.
- Insights opens with highlight tiles (spending pace, categories above their usual with a link to those transactions, and how much of the last three months' income you kept). A deposit tagged as recurring income counts as income in cash flow even if its category isn't Income.
- Search results can be sorted newest or oldest first, by merchant A to Z, or by largest or smallest amount.

- Insights tab: net worth by month back to the start of your YNAB history (Plaid balances from each account's connection on, the linked YNAB accounts before that, YNAB-only accounts, and accounts you add by hand), with an interactive chart, 1Y/3Y/5Y/All ranges, assets and debts, and a month-by-month table. Spending by month with a category donut and list (each links to Search), compared with your average, and the largest purchases. Cash flow by month: income, spending, debt payments, saving, and how much you kept. A few plain observations (spending pace, categories above their usual, share of income kept).
- Categories have a "Counts as" setting (Spending, Income, Saving or investing, Debt payment, Transfer) that Insights uses. Income, Loans, and Transfers and payments start as Income, Debt payment, and Transfer.
- Accounts added by hand (retirement, investment, cash, property, other asset, loan, other debt) with balances entered whenever you check them; YNAB accounts with no Plaid connection can take a newer balance the same way.
- Saving on a page that reloads itself (Bills, Accounts) no longer sends you to the Inbox.

- Dark, Apple-style design: glass cards, rounded corners, a floating tab bar on phones, narrowed SF Pro headings on Apple devices, and animations for syncing and reviewing.
- Inbox: a collapsed Scheduled list of every upcoming bill (next due at the bottom) that follows the account filter, unreviewed transactions on top with one-tap review and mark all, then every transaction grouped by day, with paging and an account filter (all accounts, or one at a time; filters reset when the page reloads).
- Every edit or add (transactions, bills, paydays, categories, account names) opens in the same pop-up sheet. Amount fields fill in from the cents as you type (1500 becomes 15.00).
- Tap a transaction to rename the merchant, change its category (for every transaction from that merchant, or just this one), and add a note. Merchant renames and categories apply to past and future transactions.
- Your own categories (a starter list mapped from Plaid's categories, then fully editable, with named or custom hex colors), renamable accounts, and colored recurring tags for transactions that match a bill.
- When a pending charge posts, its review, note, and one-off category carry over; a changed final amount shows as a tip (Restaurants category) or a difference. Any transaction can also take a tip entered by hand (for example, a haircut), in dollars or as a percent of the price before the tip, each filling in the other, and the CSV export has a tip column, and a jump of more than 30% goes back to the inbox.
- Card payments and transfers between your accounts are paired (same amount, opposite sides, within 5 days, at least one side checking): both show a ⇄ tag naming the other account, reviewing one reviews both, and search totals leave them out.
- YNAB names: posted transactions on linked accounts take the payee and memo of the matching YNAB transaction (runs once after upgrading; Compare has Apply again). Merchants with two or more matches on one payee are renamed for the future; others get one-off names. Your own renames and notes win.
- Transaction editor: one choice ("All from this merchant" or "Just this one") covers the name and category, a list of similar merchants (same first word) you can include one by one, unchecked by default, name suggestions while typing, and a Bill or income picker (automatic, none, a specific one, or a new one filled in from the transaction). Saving updates the list in place without losing your scroll position.
- Recurring income (paychecks every 2 weeks, interest) next to bills, with averages for varying amounts. Income shows in Scheduled and never changes coverage. Mark one as your paycheck and its dates become your paydays for coverage (the manual payday setting is the fallback).
- A "Pending only" toggle on the home list shows every pending transaction, reviewed or not, for the chosen account.
- Accounts lists every renamed merchant with the bank's name and transaction count, and Undo restores the bank's name.
- Automatic bill tags check amount and timing, not just the merchant: a fixed bill needs a close amount plus a repeat about one cycle away (or its scheduled date), or the exact amount repeating one cycle away (an older price). Varying bills need their scheduled date or a charge about a cycle away. One-off orders from the same merchant are not tagged.
- Archive a bill or income when it ends: it leaves coverage and Scheduled, moves to an Archived list, and keeps tagging charges up to the archive date. Restore brings it back; Delete removes it and its tags.
- Accounts can have a short name (used on filter pills) and be marked Everyday, Less used (behind More in the filters, last in lists), or Archived (hidden from filters and lists; history stays).
- The transaction editor shows Plaid's details when the bank provides them: time, purchase and posted dates, address with a Show in Maps button (Apple Maps), website, merchant logo, payment app, transfer details, and the original bank description. Amazon purchases link to Amazon's payment history and Apple App Store and subscription charges to Apple's purchase history.
- Search: quick picks for each month and the last five years (a month and a year together mean that month of that year), dates that stay empty until tapped, several accounts or categories at once, spent or received, has a note, tagged as a recurring bill or income, includes a tip, pending, amount changed since pending, and transfers only.
- Bills shows the bills due before payday with their date, frequency, and card.
- Each bank connection has an Accounts button to share another account from that bank (Plaid account selection). New connections list their accounts right away.
- Moving between Inbox, Bills, Insights, Search, and Accounts slides the tab highlight (view transitions where supported).
- Every sync keeps each account's balance for the day (balance_history), for net worth trends.
- Home screen and browser icons, and a web app manifest.
- Bills with detection, editing, tag colors (named or custom hex), varying amounts shown as an average (≈), the card each bill is paid with (the card of its last charge unless you pick one), and coverage through the next payday; search with CSV export; Plaid versus YNAB comparison.
