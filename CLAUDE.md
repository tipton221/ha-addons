# CLAUDE.md

Public repository of Mike's personal Home Assistant add-ons. Home Assistant installs them from this repository's URL.

## Rules

- This repository is public. Never commit personal details, domains, account names, transaction data, tokens, keys, or option values. Those belong in the private [Technology repository](https://github.com/tipton221/technology), which also holds the design notes (for Finance: `docs/finance-app.md`).
- Each add-on lives in its own folder with `config.yaml`, `Dockerfile`, and `README.md`, and is listed in the root `README.md`.
- Bump an add-on's `version` in `config.yaml` with every change, or Home Assistant will not offer the update.
- Pin dependency and base image versions.
- Changes go through a `claude/*` branch and a pull request. Do not merge unless Mike asks.

## Dates and times

- Mike is in Seattle. Every date or time he gives is Pacific time. The session clock runs in UTC; check `TZ=America/Los_Angeles date` before writing a date.

## Finance add-on

- Tests: `python -m pytest finance/tests` (needs the packages in `finance/requirements.txt` plus pytest).
- Local run: see `finance/README.md`.
