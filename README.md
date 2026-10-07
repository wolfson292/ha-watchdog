# ha-watchdog

An external watchdog for Home Assistant OS. It runs as a container on another
Docker host, notices when Home Assistant is wedged, alerts you through Pushover,
and restarts it over SSH with exponential backoff so a bad state can't turn into
a restart loop.

It targets a failure HA's own Supervisor watchdog misses: Core is still running
and `/` still answers, but its executor thread pool is exhausted. The UI sits on
"Loading...", API calls time out, and integrations such as MQTT stop
reconnecting.

## Checks (every 60s)

| Probe | Healthy | Why |
|---|---|---|
| `GET /api/` | 401 within 15s | Needs Core's executor; hangs when Core is wedged even though `/` still answers |
| `GET /static/icons/favicon.ico` (gzip) | 200 within 15s | The same failure leaves browsers stuck on "Loading..." |
| MQTT canaries (optional) | at least one updated in the last 10 min | Catches the MQTT client stuck disconnected while the broker is fine |

## Actions

1. Three failed web checks in a row open an incident and send an alert.
2. `ha core restart` over SSH (falls back to `docker restart homeassistant`), then a 10-minute grace period.
3. Still failing: `ha host reboot`, then a 15-minute grace period; repeats host reboots.
4. MQTT stale but web fine: first reloads the MQTT integration, then escalates the same way.
5. **Backoff:** the gap between actions doubles with each action in the last 24h
   (15m, 30m, 1h, 2h, capped at 4h). After 6 actions in 24h it stops acting and only alerts.
6. Hourly reminders while unhealthy, and a recovery alert with the downtime and actions taken.
7. If HA *and* the configured gateway are unreachable, it assumes its own network is down and does nothing.
8. If HA's SSH port is closed, it alerts that the VM needs a hard reset.

## Requirements

- Home Assistant OS with the **Advanced SSH & Web Terminal** add-on. Its Supervisor
  role must allow `ha core restart` / `ha host reboot`, and Docker access is needed for the
  `docker restart` fallback (protection mode off).
- A Docker host on the same network to run the watchdog.
- A Pushover account and application token (optional, but alerts are only logged without it).

## Setup

1. Configure: copy `.env.example` to `.env` and fill it in, or enter the same
   variables as Portainer stack environment variables. `HA_URL` is the only required one.
2. Deploy `docker-compose.yml`, which uses the prebuilt image: `docker compose up -d`,
   or paste it into a Portainer stack.
3. Authorize the watchdog's SSH key. On first start it generates a key pair in the
   `ha-watchdog-data` volume and logs the public key:
   ```bash
   docker logs ha-watchdog 2>&1 | grep "SSH public key"
   ```
   Add that key to the Advanced SSH add-on's `authorized_keys` and restart the add-on.
4. Restart the watchdog. Its startup alert reports whether SSH and the `ha` CLI work,
   whether the MQTT check is on, and whether it is in dry-run mode.

To try it safely, set `DRY_RUN=1`: it alerts about the actions it would take
without running them.

## Configuration

Common settings (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `HA_URL` | required | Home Assistant base URL |
| `SSH_HOST` / `SSH_PORT` / `SSH_USER` | host from `HA_URL` / `22` / `hassio` | Advanced SSH add-on login |
| `GATEWAY` | unset | Router IP for the "is it me?" check |
| `PUSHOVER_USER` / `PUSHOVER_TOKEN` | unset | Pushover alerts |
| `HA_TOKEN` + `MQTT_CANARIES` | unset | Enables the MQTT freshness check |
| `DRY_RUN` | `0` | `1` = alert only |

Thresholds, grace periods and backoff are in `DEFAULTS` at the top of
`watchdog.py`, and any of them can be overridden with an environment variable of
the same name.

## Image

The image is `ghcr.io/wolfson292/ha-watchdog` (linux/amd64 and linux/arm64),
built by GitHub Actions from the `Dockerfile`:

| Tag | Built from |
|---|---|
| `latest` | every push to `main` |
| `1.2.3`, `1.2` | a `v1.2.3` git tag |
| `sha-<commit>` | every build |

Pin a version tag in `docker-compose.yml` if you'd rather upgrade deliberately.

## Development

Edit `watchdog.py`, push to `main`, and wait for the "Build image" workflow. Then
pull and recreate the container: `docker compose pull && docker compose up -d`, or
in Portainer, update the stack with **Re-pull image** enabled.

To build locally:

```bash
docker build -t ha-watchdog .
```
