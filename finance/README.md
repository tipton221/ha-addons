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

Version 2.2.37:

- The native app's Plaid page shows nothing of its own while Plaid opens (no "Opening Plaid" note), only an error if one happens, and closes as soon as Plaid succeeds.

Version 2.2.36:

- The native app's Plaid page hides its message card while the card is empty, so no blank box shows under Plaid's loading spinner.

Version 2.2.35:

- Plaid webhooks: Plaid posts to `/plaid/webhook` when it has new data, and the server syncs about 10 seconds later (one sync covers a burst). The path skips the login, so each message must carry Plaid's signature (the `Plaid-Verification` JWT, checked with Plaid's key, under five minutes old, over this exact body), and only a known connection starts a sync. New connections get the webhook address from Link and existing ones through `/item/webhook/update`. With an https `public_url`, the scheduled sync runs hourly as a backup. Behind Cloudflare Access, the path needs a bypass, like `/.well-known/apple-app-site-association`.
- `plaid_updates` in `GET /api/accounts`: how new Plaid data arrives, with the last webhook.

Version 2.2.31:

- A pending charge that posts keeps what was done to it (review, note, one-off name and category, bill, tip) even when the bank doesn't tell Plaid which posted charge replaced it: the pending one is kept for two weeks after it's removed and matched to a posted charge on the same account with the same amount (or more, up to a tip), dated from 3 days before to 14 days after.
- `posted` in `GET /api/inbox`: charges that posted in the last week (once reviewed), until dismissed with `POST /api/posted/seen`.
- Native app notifications work like one app across devices: when the count of things to review or fix changes, every device's badge is set (a badge-only push), and at zero a silent push clears the notifications shown. `POST /api/push/opened` clears a notification from the other devices when it's opened on one. The "All caught up" notification is now for Web Push only.

Version 2.2.30:

- Removed what only the native app used before its screens went native: Accounts starting Plaid from `?link=` (the app uses `/link`) and Bills opening a filled-in form from `?from_source=&from_id=` (the app's Bills does that itself).

Version 2.2.29:

- `GET /api/insights` for the native app's Insights: highlight tiles, net worth by month with today's accounts, investments with each period's gains, contribution limits, loans, the last 12 months of spending and cash flow by category, and the lines the page shows. `GET /api/insights/spending?m=&format=json` gives a month's spending as transaction rows.

Version 2.2.28:

- `GET /api/search` for the native app's Search: the same filters as the page (in the address), the results by day (or one dated list for other orders), the totals line, and the account, category, sort, and year choices.

Version 2.2.27:

- In the native app, the `/link` page is see-through, so Plaid's box shows straight over the app's own screen; an error closes it after a moment to read.

Version 2.2.26:

- `/link?link=…` runs Plaid Link on its own page (no Accounts page behind it) for the native app's sheet, and tells the app when Plaid finishes or closes, so the sheet closes itself.

Version 2.2.25:

- `GET /api/accounts` for the native app's Accounts: accounts, bank connections (with their actions), categories, renamed merchants, settings, and the server's version, with the lines the page shows.
- Accounts starts Plaid Link from `?link=connect|invest|reconnect|accounts|investments` (`&item=` for a connection), so the native app can open it in a sheet.

Version 2.2.24:

- `GET /api/bills` for the native app's Bills: coverage, payments in transit, due before payday, paydays, bills, income, and archived ones, each with the line the page shows and the editor's values, plus the accounts and colors the editor offers. "Find new bills" also returns its candidates as data, and `GET /api/bills/prefill` gives a new bill's starting values from a transaction.

Version 2.2.23:

- Bills opens a new bill form filled in from a transaction when given `?from_source=&from_id=` (the native app's "Make it a bill").

Version 2.2.22:

- `GET /api/inbox` for the native app's Inbox: the summary, To review, the Scheduled summary, account filters, and All transactions by day with the same cursor paging as the page (`before=`). It is built from the same data as the page, and rows mirror `_txn.html` with amounts formatted and colors as hex.

Version 2.2.21:

- The tab bar puts Accounts before Search, so Search is last.
- The Mac app marks the page `native-sidebar`, which hides the Inbox's sync button (the sidebar has one).

Version 2.2.20:

- `GET /api/sidebar` for the Mac app's sidebar: the Inbox summary (left through the next payday), the last sync time, and the everyday accounts with their balances.

Version 2.2.19:

- Sign in another device with a one-time code: Accounts shows a code (eight characters, five minutes, one use; five wrong tries cancel it), and the sign-in page takes it under "Use a sign-in code". For devices without the passkey, such as the iPhone simulator.

Version 2.2.18:

- The native apps hide the site's own tab bar: they mark the page `native-tabs`, since the page's content security policy blocks a style added from outside.

Version 2.2.17:

- Standalone server: checks for updates every minute, and the bottom of Accounts shows the running version, an update being installed (the page reloads when it's done), or one that didn't install.

Version 2.2.16:

- For the native app's own tab bar: inside the app, opening a page no longer jumps to the Inbox (each tab opens its own page), and the page reports the Inbox and Accounts counts separately.

Version 2.2.15:

- Push notifications for the native app through Apple (APNs): the app registers its device, and every notification (new transactions, broken connections, the test, "All caught up") goes to both Web Push and app devices. The key file is `apns.p8` in the data folder; option `apns_key_id`.
- Inside the native app, the page hands the badge count to the app, and the Notifications card explains that the app handles notifications.

Version 2.2.14:

- Serves `/.well-known/apple-app-site-association` (before the login checks) when the new `apple_app_id` option is set, so a native app with that ID can use the site's passkeys. Cloudflare Access needs a bypass for that one path.

Version 2.2.13:

- Accounts › Full backup downloads a consistent copy of the whole database (including bank connections), for moving the app to another server. Only a signed-in session can request it.

Version 2.2.12:

- The pinned day header is frosted glass (a strong blur with a translucent tint) from the top edge down, instead of a solid fill, on phones and wide screens.

Version 2.2.11:

- Wide screens get the same pinned day header treatment: it fills the space up to the top edge behind the tab bar, so rows no longer show around it.
- Paydays twice a month take an optional first payday (for a new job): no payday is counted before it, so coverage runs to that date.
- An IRA can name a recurring bill (the monthly transfer from a cash account) in its contributions sheet: transfers tagged with it count toward the IRA limit, for providers that don't report contributions to Plaid. A contribution also reported by Plaid or YNAB (same amount within 5 days) counts once.

Version 2.2.10:

- On phones the pinned day header extends up to the top edge while it is pinned, so transactions no longer show between it and the status bar.

Version 2.2.9:

- The pinned day header sits a little lower on phones, clear of the blur under the iPhone status bar.

Version 2.2.8:

- Transactions listed by date (Inbox and Search) are grouped into a neutral panel per day, and the day's date stays pinned at the top of the screen while you scroll through it. A day split by Show more joins back into one panel.

Version 2.2.7:

- Tab badges look like iOS badges: no dark outline, slightly larger, with a lighter number.

Version 2.2.6:

- Search remembers the last search (text, filters, and sort) and where you were in the results while you move between tabs. Clear, reloading, or reopening the app starts over.

Version 2.2.5:

- In transit items: the accounts use the full width (smaller on phones), the amount sits beside the date, and the status pill has its own line so it doesn't wrap. The panel header drops its explanation line and only shows a line when something has no match after 7 days.

Version 2.2.4:

- Editing a transaction applies to just that one by default; choose "All from this merchant" to change every transaction from it.
- Bills: In transit is a collapsible panel like the Inbox's Scheduled panel, with a count and the total being counted. It opens on its own when something has gone 7 days without a match.

Version 2.2.3:

- The Scheduled panel's "Next" line gets its own row across the panel and wraps instead of being cut off.

Version 2.2.2:

- The Inbox's Scheduled list collapses into a tinted panel with a calendar icon, the count, the next bill and its date, and the totals, so it's hard to miss.

Version 2.2.1:

- Reaching zero (nothing to review or fix) on a device closes that device's Finance notifications and clears its icon badge.
- Optional, in Accounts › Notifications: when you're caught up on one device, the others get a quiet "All caught up" notification in place of the old one, with the badge cleared. Sent once until something new is announced. Every notification now shares one tag, so a newer one replaces the last.

Version 2.2.0:

- Notifications (Web Push) for the Home Screen (iPhone) or Dock (Mac) app, turned on per device in Accounts › Notifications. After each sync: new transactions to review (one shows its merchant, amount, and account; several are listed) and a bank connection that needs attention. Each transaction is announced once, and what was already waiting when notifications were turned on isn't. A test button sends one.
- The app icon badge (the Inbox count plus the Accounts count) is set with every notification and whenever the app is open, so it stays current. Safari requires every push to show a notification, so there are no silent badge-only updates.
- Built on the standard (RFC 8291 encryption, RFC 8292 VAPID) with the packages the app already uses; the signing key is made once and kept in the database.

Version 2.1.2:

- The Accounts tab shows a red badge with the number of things to fix: a bank connection that needs signing in again or failed, one waiting for investment access, or a failed YNAB sync.

Version 2.1.1:

- The Inbox tab shows a red badge with the number of transactions to review, on every page, and it counts down as you review them.

Version 2.1.0:

- Bills can repeat every N months or every N years (for example a license renewed every 2 years).
- "Date varies" for bills paid any time in a window before a deadline (a registration or license renewal): set the due date and how many months early it can be paid. A payment anywhere from the window's opening to a month after the due date counts and is tagged; the next due date follows the schedule instead of the day it was paid; the bill doesn't look ended while the window is open; and coverage sets the amount aside from the day the window opens until it's paid.

Version 2.0.6:

- Net worth by month spans the card: month left, net worth centered, change right.

Version 2.0.5:

- Loan stats line up with the progress bar: the first flush left, the middle centered, the last flush right.
- A loan's rate, payment, and balance date are separated by dots.

Version 2.0.3:

- A loan's balance keeps itself current between the balances you enter: from the latest one, interest accrues daily and each payment tagged with the loan's bill comes off as it posts. Net worth uses the same estimate. Entering a balance from the servicer resets it.
- Loan progress reads "29% paid off" with the dollars beside it, and the bar has even spacing above and below.

Version 2.0.2:

- Insight tiles always pair up two per row: when there's an odd number, one more is added (money saved or invested over the last three months, else a category well below its usual last month, else last month's top category).

Version 2.0.1:

- Every loan shows a progress bar (paid so far of its original amount, a new optional field), payments left, the payoff month, and the total still to pay with its interest, not only loans working toward PSLF.
- The PSLF count, its date, and the recertification date sit under the PSLF checkbox and are greyed out until it's checked. Optional dates stay empty ("None") until tapped instead of showing today's date.

Version 2.0.0: the transaction logger becomes a full financial dashboard. Insights covers net worth back to the start of YNAB, spending and cash flow, investments (Plaid Investments, gains and Modified Dietz returns by period, a balance chart, contribution limits), and loans (payoff projections and PSLF tracking). Changes since 1.14.1:

- A debt entered by hand counts in net worth before its first balance too, so adding an existing loan doesn't look like a drop in net worth that month. With a rate and payment the balance is worked backward month by month (each earlier month adds back a payment less its interest); without them the first balance carries back as is.

Version 1.14.1:

- Investments: a balance chart (week by week) for the chosen period, and each account's name on its own line across the full width.
- Loans: a cleaner card: name and balance, then rate and payment, then PSLF progress in its own block with payments left, the last payment's month, and the amount forgiven.
- A loan's month choices (counts or not) save together with the rest of its form instead of one at a time.

Version 1.14.0:

- Insights › Loans: each loan's balance, rate, and payment, with a payoff date projected month by month. A loan added here is a manual debt account, so it counts in net worth; a Plaid loan account can carry the same details.
- Public Service Loan Forgiveness: the count from studentaid.gov (and its date) plus one for each later month with a payment to the loan's recurring bill, at most one a month. Any month can be marked as not counting. Shows payments left, the month of the 120th, what's still to pay, and the balance projected to be forgiven.
- The next income-driven recertification date, highlighted within 60 days.

Version 1.13.2:

- Plaid Investments history is requested in full (24 months) on every sync. Right after Investments is added Plaid may still be loading it, and a shorter window never picked it up.
- IRA contributions also come from YNAB transfers into the IRA's linked YNAB account, before Plaid's activity starts. Each shows where it came from.
- Each investment account's sheet lists its latest activity from Plaid with type and subtype.
- Sheet buttons are all the same height. Short endings of account names ("403 (b)") stay on the line with the word before them.

Version 1.13.1:

- Investments: an account with balances entered by hand uses only those entries (and their money put in) for gains, not YNAB history where contributions can't be told from growth. A period whose start has no entered balance within about a month shows "Needs a balance near" that date instead of a made-up gain. History that starts inside a period is measured from its first balance.
- Contribution limits: transfers into an IRA count as contributions (some providers report contributions that way). Each one shows its description and can be set to count for its year, the year before (January 1 to April 15), or not at all.
- Holdings show cash as Cash. Sheet buttons no longer touch the one above.
- Scripts and styles load with a version in their URL, so an update never runs with a cached old copy.

Version 1.13.0:

- Plaid Investments: "Add investments" on a connection with investment accounts asks Plaid for consent and then syncs holdings and up to 24 months of investment activity. "Connect it for investments only" links a retirement plan site that has no bank transactions (Investments product only).
- Insights › Investments: balance, gain, and return for 3M, 6M, YTD, 1Y, and All, per account and in total. Gain leaves out money put in or taken out (Plaid contributions and withdrawals, YNAB transfers, or the "money put in" entered with a manual balance, spread evenly over the weeks since the previous entry). Return is the Modified Dietz return. Each account's sheet shows every period and its holdings with gain against cost basis. Any asset account can be added or hidden.
- Insights › Contribution limits: IRA contributions (from Plaid Investments activity) against the IRS limit, with contributions made January 1 to April 15 switchable to the year before. 403(b)/401(k) contributions entered by hand from a paystub. Limits for 2025 to 2027, under-50 amounts.

Version 1.12.2:

- In transit items show their status as a pill: blue while waiting on (or pending in) the other account, orange when nothing matched after 7 days.

Version 1.12.1:

- An "In transit" pill on transactions counted as in transit, in the Inbox and Search lists.
- Each in-transit item on the Bills page shows its transaction (name, category, date, account, bank description, note).
- Only transactions in a category that counts as a transfer (Transfers and payments) can be in transit, using the category the lists show, so a recategorized one is left alone.
- The other account is guessed only from past paired transfers with the same bank description, not from any transfer on the account, which flagged fees and unrelated transfers.

Version 1.12.0:

- Money in transit: when a card payment or transfer shows on one account but not yet the other (in either order), balances count it as if both sides had posted, for up to 7 days. The other account is worked out from where past transfers like it went, and can be changed or dismissed on the Bills page. Coverage, the Inbox summary, and today's net worth all use the adjusted balances. After 7 days with no match it is listed but no longer counted.
- Transfers pair when the two sides post up to 7 days apart (was 5).
- Find new bills shows what it found and asks first: tick the ones to add, or mark one "Not a bill" so it isn't suggested again.
- Search also matches a transaction's bill name.
- Cash flow and net worth table headings sit exactly over their columns (the smaller heading text was narrowing the first column).
- Tab bar: the highlight moves the moment a tab is pressed, and page transitions are shorter.

- Bills learn from your choices: setting a transaction to a bill by hand adds that transaction's bank merchant to the bill, so its other charges (same amount, about a cycle apart, a few days either way) get the tag automatically even when the bank's name doesn't contain the bill's match text.
- Cash flow and net worth tables: headings and numbers centered on each column, the month in bold, and a narrower month column so the number columns have more room.

- Insight tiles: red for worse than usual, green for better, gray for neutral. More room between the numbers at the top of Net worth and Spending.
- Cash flow: Kept is now Net, with its share of income on a second line on every row so rows are the same height. Saved also counts transfers Plaid marks as going to (or coming from) a retirement or investment account, such as a Roth IRA contribution, even when that account isn't connected.
- Accounts › Categories shows what each category counts as in a colored pill (Spending, Income, Saving, Debt, Transfer), one color per kind.

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
