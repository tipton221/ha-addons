# Finance server (without Home Assistant)

Runs the same Finance container as the Home Assistant add-on on an ordinary Ubuntu 24.04 machine, such as a small cloud server. The app's code and behavior are the same; only the host changes.

## Setup

On a fresh server, as root:

```sh
curl -fsSL https://raw.githubusercontent.com/tipton221/ha-addons/main/server/setup.sh -o setup.sh && bash setup.sh
```

The script installs Docker and git, clones this repository to `/opt/finance-server/src`, asks for the app's options (the same ones the add-on has; secrets are not shown as you type), saves them to `/opt/finance-server/data/options.json` (readable by root only), builds the container, and starts it. It ends with a short status that has no secrets in it.

- No ports are opened. The container's bundled Cloudflare Tunnel is the only way in, as with the add-on.
- The database and options live in `/opt/finance-server/data`, which is kept across updates and reruns.
- Running `setup.sh` again keeps the data. Each option shows what's saved; Enter keeps it and a single dash clears it. Changing options restarts the app with them.

## Updates

A systemd timer (`finance-update.timer`) runs `update.sh` every 15 minutes. When the `main` branch has a new commit, it builds that version and swaps the container. It also keeps `update.sh`, `status.sh`, and `restore.sh` in `/opt/finance-server` current. Only one update runs at a time. If the build fails, or the new container doesn't finish starting within about a minute, the previous version keeps running and that commit isn't tried again until there is a newer one. To retry by hand: `/opt/finance-server/update.sh force`.

## Moving an existing app here

1. In the running app, open Accounts › Full backup and download the file. It holds the bank connections: keep it private and delete it afterward.
2. Stop the old copy (for the add-on: stop it and turn off "Start on boot"), so only one copy syncs and sends notifications.
3. Copy the file to the server, for example `scp finance-backup-YYYY-MM-DD.db root@SERVER:/root/`.
4. On the server: `/opt/finance-server/restore.sh /root/finance-backup-YYYY-MM-DD.db`. It checks the file, keeps the current database next to it (`finance.db.before-restore-<time>`), swaps the backup in, restarts, and prints the status.
5. Run `bash setup.sh` again to set the web address and the Plaid and YNAB keys, keeping the rest.
6. Point the web address at this server's tunnel. Passkeys belong to the web address, so they keep working when the address stays the same.

## Useful commands

```sh
/opt/finance-server/status.sh         # short status, no secrets
docker logs --tail 50 finance          # app and tunnel logs
systemctl list-timers finance-update   # next update check
/opt/finance-server/update.sh force    # rebuild and restart now
```
