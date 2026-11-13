# CLAUDE.md

Public repository of Mike's personal Home Assistant add-ons. Home Assistant installs them from this repository's URL.

## Rules

- This repository is public. Never commit personal details, domains, account names, transaction data, tokens, keys, or option values. Those belong in the private [Technology repository](https://github.com/tipton221/technology), which also holds the design notes (for Finance: `docs/finance-app.md`).
- Each add-on lives in its own folder with `config.yaml`, `Dockerfile`, and `README.md`, and is listed in the root `README.md`.
- Bump an add-on's `version` in `config.yaml` with every change, or Home Assistant will not offer the update. The exception is a Dependabot update: after it merges, `.github/workflows/bump-version.yml` raises the patch version automatically when a push changes `finance/Dockerfile` or `finance/requirements.txt` without changing `config.yaml`.
- Dependabot (`.github/dependabot.yml`) checks the Docker images, Python packages, and GitHub Actions on a schedule, waiting 7 days after a release before proposing it. Python minor and major upgrades are ignored and done by hand. `.github/workflows/test.yml` runs the tests and an image build on every pull request.
- Pin dependency and base image versions.
- Changes go through a `claude/*` branch and a pull request. Do not merge unless Mike asks.

## Dates and times

- Mike is in Seattle. Every date or time he gives is Pacific time. The session clock runs in UTC; check `TZ=America/Los_Angeles date` before writing a date.

## Finance add-on

- Tests: `python -m pytest finance/tests` (needs the packages in `finance/requirements.txt` plus pytest).
- Local run: see `finance/README.md`.
