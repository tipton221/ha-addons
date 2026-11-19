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
- Running `setup.sh` again keeps the data and asks whether to replace the options.

## Updates

A systemd timer (`finance-update.timer`) runs `update.sh` every 15 minutes. When the `main` branch has a new commit, it builds that version and swaps the container. If the build fails, or the new container doesn't finish starting within about a minute, the previous version keeps running and that commit isn't tried again until there is a newer one. To retry by hand: `/opt/finance-server/update.sh force`.

## Useful commands

```sh
docker logs --tail 50 finance          # app and tunnel logs
systemctl list-timers finance-update   # next update check
/opt/finance-server/update.sh force    # rebuild and restart now
```
