#!/usr/bin/env python3
"""Home Assistant watchdog.

Runs next to (not on) a Home Assistant OS install, probes it, alerts via
Pushover, and restarts HA over SSH (Advanced SSH add-on) with exponential backoff.

Failure modes it targets:
  * "wedged" Core: main loop alive but the executor thread pool is exhausted,
    so /api/ and static files hang while / still answers.
  * MQTT client stuck disconnected while the broker is fine.
  * Core or the whole VM down.

Remediation ladder per incident:
  MQTT stale (API ok)  -> reload the MQTT config entry via the REST API
  web probes failing   -> `ha core restart` (fallback: `docker restart homeassistant`)
  still failing        -> `ha host reboot`
  every action is rate limited: the gap since the previous action doubles with
  each action in the last 24h (BACKOFF_BASE .. BACKOFF_MAX) and at most
  MAX_ACTIONS_24H actions run per 24h; after that it only alerts.

Stdlib only. All settings come from environment variables (see DEFAULTS).
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULTS = {
    "HA_URL": "",                         # required, e.g. http://homeassistant.local:8123
    "HA_TOKEN": "",                       # optional long-lived token, enables MQTT check
    "MQTT_CANARIES": "",                  # comma separated entity_ids fed by MQTT
    "MQTT_MAX_AGE": "600",                # canary considered stale after N seconds
    "SSH_HOST": "",                       # defaults to the host in HA_URL
    "SSH_PORT": "22",
    "SSH_USER": "hassio",                 # Advanced SSH add-on default
    "SSH_KEY": "/data/id_ed25519",
    "GATEWAY": "",                        # optional: tells "HA down" from "watchdog host offline"
    "PUSHOVER_USER": "",
    "PUSHOVER_TOKEN": "",
    "CHECK_INTERVAL": "60",
    "PROBE_TIMEOUT": "15",
    "FAIL_THRESHOLD": "3",                # consecutive failed checks before acting
    "MQTT_FAIL_THRESHOLD": "5",
    "CORE_GRACE": "600",                  # wait after core restart before judging
    "REBOOT_GRACE": "900",                # wait after host reboot before judging
    "MQTT_GRACE": "300",
    "BACKOFF_BASE": "900",
    "BACKOFF_MAX": "14400",
    "MAX_ACTIONS_24H": "6",
    "REMIND_EVERY": "3600",
    "DRY_RUN": "0",                       # 1 = alert only, never restart
    "STATE_FILE": "/data/state.json",
    "HEARTBEAT_FILE": "/tmp/heartbeat",
}


def cfg(name):
    return os.environ.get(name, DEFAULTS[name]).strip()


def cfg_int(name):
    return int(cfg(name))


HA_URL = cfg("HA_URL").rstrip("/")
HA_TOKEN = cfg("HA_TOKEN")
CANARIES = [c.strip() for c in cfg("MQTT_CANARIES").split(",") if c.strip()]
DRY_RUN = cfg("DRY_RUN") == "1"
HA_HOST = urllib.parse.urlparse(HA_URL).hostname
HA_PORT = urllib.parse.urlparse(HA_URL).port or 8123
SSH_HOST = cfg("SSH_HOST") or HA_HOST


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {msg}", flush=True)


def fmt_dur(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------- alerts

def notify(title, message, priority=0):
    log(f"ALERT[{priority}] {title}: {message}")
    user, token = cfg("PUSHOVER_USER"), cfg("PUSHOVER_TOKEN")
    if not (user and token):
        log("  (Pushover not configured, alert only logged)")
        return
    data = urllib.parse.urlencode({
        "token": token, "user": user, "title": title,
        "message": message[:1000], "priority": priority,
    }).encode()
    try:
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", data, timeout=20).read()
    except Exception as exc:
        log(f"  Pushover send failed: {exc}")


# ---------------------------------------------------------------- probes

def tcp_open(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_reachable():
    gw = cfg("GATEWAY")
    if not gw:
        return True
    return tcp_open(gw, 80, 3) or tcp_open(gw, 443, 3)


def http_status(path, headers=None, timeout=None):
    req = urllib.request.Request(HA_URL + path, headers=headers or {})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg_int("PROBE_TIMEOUT")) as resp:
            resp.read()
            return resp.status, time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return exc.code, time.monotonic() - start
    except Exception as exc:
        return f"{type(exc).__name__}", time.monotonic() - start


def probe_web():
    """Returns (ok, detail). Each probe needs Core's executor, unlike GET /."""
    if not tcp_open(HA_HOST, HA_PORT):
        return False, f"port {HA_PORT} closed"
    results = []
    # Unauthenticated /api/ must answer 401 quickly.
    code, secs = http_status("/api/")
    results.append(("api", code == 401, code, secs))
    # Static file with compression, which is what browsers request.
    code, secs = http_status("/static/icons/favicon.ico", {"Accept-Encoding": "gzip, br"})
    results.append(("static", code == 200, code, secs))
    ok = all(r[1] for r in results)
    detail = ", ".join(f"{n}={c} in {s:.1f}s" for n, _, c, s in results)
    return ok, detail


def get_state(entity_id):
    req = urllib.request.Request(
        f"{HA_URL}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {HA_TOKEN}"})
    with urllib.request.urlopen(req, timeout=cfg_int("PROBE_TIMEOUT")) as resp:
        return json.load(resp)


def probe_mqtt():
    """Returns (ok, detail), or (None, reason) when the check is not configured."""
    if not (HA_TOKEN and CANARIES):
        return None, "not configured"
    now = datetime.now(timezone.utc)
    fresh, notes = 0, []
    for ent in CANARIES:
        try:
            st = get_state(ent)
        except Exception as exc:
            notes.append(f"{ent}: {type(exc).__name__}")
            continue
        updated = datetime.fromisoformat(st["last_updated"].replace("Z", "+00:00"))
        age = (now - updated).total_seconds()
        if st["state"] not in ("unavailable", "unknown") and age <= cfg_int("MQTT_MAX_AGE"):
            fresh += 1
        else:
            notes.append(f"{ent}: {st['state']} {fmt_dur(age)} old")
    return fresh > 0, (f"{fresh}/{len(CANARIES)} canaries fresh" + (f" ({'; '.join(notes)})" if notes else ""))


# ---------------------------------------------------------------- actions

def ssh(command, timeout):
    # Non-interactive sessions don't get the add-on's SUPERVISOR_TOKEN, which the
    # ha CLI needs; load it from the s6 container environment.
    command = ('export SUPERVISOR_TOKEN="$(cat /run/s6/container_environment/SUPERVISOR_TOKEN)"; '
               + command)
    args = [
        "ssh", "-i", cfg("SSH_KEY"), "-p", cfg("SSH_PORT"),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/data/known_hosts",
        "-o", "ServerAliveInterval=15",
        f"{cfg('SSH_USER')}@{SSH_HOST}", command,
    ]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return p.returncode == 0, out[-400:]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"


def do_core_restart():
    ok, out = ssh("ha core restart", timeout=300)
    if ok:
        return True, "ha core restart"
    log(f"ha core restart failed ({out}); falling back to docker restart")
    ok2, out2 = ssh("docker restart -t 60 homeassistant", timeout=180)
    if ok2:
        return True, "docker restart homeassistant (ha CLI failed)"
    return False, f"ha core restart: {out}; docker restart: {out2}"


def do_host_reboot():
    ok, out = ssh("ha host reboot", timeout=120)
    # The connection usually drops mid-command once the reboot starts.
    if ok or not tcp_open(SSH_HOST, int(cfg("SSH_PORT"))):
        return True, "ha host reboot"
    return False, out


def do_mqtt_reload():
    data = json.dumps({"entity_id": CANARIES[0]}).encode()
    req = urllib.request.Request(
        f"{HA_URL}/api/services/homeassistant/reload_config_entry", data=data,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return True, "reloaded MQTT config entry"
    except Exception as exc:
        return False, f"MQTT reload failed: {exc}"


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(cfg("STATE_FILE")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = cfg("STATE_FILE") + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, cfg("STATE_FILE"))


class Watchdog:
    def __init__(self):
        s = load_state()
        self.actions = [a for a in s.get("actions", []) if a["ts"] > time.time() - 86400]
        self.incident = s.get("incident")        # dict while HA is unhealthy
        self.fail_count = 0
        self.mqtt_fail_count = 0
        self.grace_until = s.get("grace_until", 0)

    def save(self):
        save_state({"actions": self.actions, "incident": self.incident,
                    "grace_until": self.grace_until})

    # -- backoff
    def next_allowed_at(self):
        recent = [a for a in self.actions if a["ts"] > time.time() - 86400]
        if not recent:
            return 0
        gap = min(cfg_int("BACKOFF_BASE") * 2 ** (len(recent) - 1), cfg_int("BACKOFF_MAX"))
        return recent[-1]["ts"] + gap

    def budget_left(self):
        recent = [a for a in self.actions if a["ts"] > time.time() - 86400]
        return cfg_int("MAX_ACTIONS_24H") - len(recent)

    # -- incident bookkeeping
    def open_incident(self, kind, detail):
        self.incident = {"kind": kind, "since": time.time(), "level": 0,
                         "last_alert": time.time(), "gave_up": False, "detail": detail}
        notify("Home Assistant unhealthy", f"{kind}: {detail}", priority=1)
        self.save()

    def close_incident(self, detail):
        inc = self.incident
        took = [a["action"] for a in self.actions if a["ts"] >= inc["since"]]
        notify("Home Assistant recovered",
               f"Down {fmt_dur(time.time() - inc['since'])} ({inc['kind']}). "
               f"Actions: {', '.join(took) or 'none'}. {detail}")
        self.incident = None
        self.grace_until = 0
        self.save()

    def remind(self, detail):
        inc = self.incident
        if time.time() - inc["last_alert"] >= cfg_int("REMIND_EVERY"):
            inc["last_alert"] = time.time()
            notify("Home Assistant still unhealthy",
                   f"{inc['kind']} for {fmt_dur(time.time() - inc['since'])}: {detail}", priority=1)
            self.save()

    def act(self, name, func, grace, detail):
        inc = self.incident
        if self.budget_left() <= 0:
            if not inc["gave_up"]:
                inc["gave_up"] = True
                notify("HA watchdog giving up",
                       f"{cfg('MAX_ACTIONS_24H')} restarts in 24h did not fix it. "
                       f"Manual attention needed. Last state: {detail}", priority=1)
                self.save()
            return
        wait = self.next_allowed_at() - time.time()
        if wait > 0:
            log(f"backoff: next action ({name}) allowed in {fmt_dur(wait)}")
            return
        if DRY_RUN:
            ok, out = True, "DRY RUN, not executed"
        else:
            log(f"ACTION {name}")
            ok, out = func()
        self.actions.append({"ts": time.time(), "action": name, "ok": ok})
        self.grace_until = time.time() + grace
        inc["level"] += 1
        inc["last_alert"] = time.time()
        if ok:
            notify(f"HA watchdog: {name}",
                   f"{out}. Re-checking in {fmt_dur(grace)}. Trigger: {detail}", priority=0)
        else:
            notify(f"HA watchdog: {name} FAILED", f"{out}. Trigger: {detail}", priority=1)
        self.save()

    # -- main step
    def step(self):
        web_ok, web_detail = probe_web()
        mqtt_ok, mqtt_detail = (probe_mqtt() if web_ok else (None, "skipped"))
        log(f"web={'ok' if web_ok else 'FAIL'} ({web_detail}); mqtt={mqtt_ok} ({mqtt_detail})")

        healthy = web_ok and mqtt_ok is not False
        self.fail_count = 0 if web_ok else self.fail_count + 1
        self.mqtt_fail_count = 0 if mqtt_ok is not False else self.mqtt_fail_count + 1

        if healthy:
            if self.incident:
                self.close_incident(mqtt_detail if mqtt_ok else web_detail)
            return

        in_grace = time.time() < self.grace_until

        if not web_ok:
            if self.fail_count < cfg_int("FAIL_THRESHOLD") and not self.incident:
                return
            if not tcp_open(HA_HOST, HA_PORT) and not gateway_reachable():
                log("gateway unreachable too: watchdog host network problem, not acting")
                return
            if not self.incident:
                self.open_incident("web unresponsive", web_detail)
            elif self.incident["kind"] != "web unresponsive":
                self.incident["kind"] = "web unresponsive"
            if in_grace:
                return
            self.remind(web_detail)
            if not tcp_open(SSH_HOST, int(cfg("SSH_PORT"))):
                if not self.incident.get("ssh_alerted"):
                    self.incident["ssh_alerted"] = True
                    notify("HA watchdog cannot reach SSH",
                           f"SSH {SSH_HOST}:{cfg('SSH_PORT')} is closed; the VM may be hung. "
                           f"Hard reset it from the hypervisor.", priority=1)
                    self.save()
                return
            if self.incident["level"] == 0:
                self.act("core restart", do_core_restart, cfg_int("CORE_GRACE"), web_detail)
            else:
                self.act("host reboot", do_host_reboot, cfg_int("REBOOT_GRACE"), web_detail)
            return

        # Web is fine but MQTT canaries are stale.
        if self.mqtt_fail_count < cfg_int("MQTT_FAIL_THRESHOLD") and not self.incident:
            return
        if not self.incident:
            self.open_incident("MQTT stale", mqtt_detail)
        if in_grace:
            return
        self.remind(mqtt_detail)
        if self.incident["level"] == 0:
            self.act("MQTT reload", do_mqtt_reload, cfg_int("MQTT_GRACE"), mqtt_detail)
        elif self.incident["level"] == 1:
            self.act("core restart", do_core_restart, cfg_int("CORE_GRACE"), mqtt_detail)
        else:
            self.act("host reboot", do_host_reboot, cfg_int("REBOOT_GRACE"), mqtt_detail)


def ensure_key():
    key = cfg("SSH_KEY")
    if not os.path.exists(key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ha-watchdog",
                        "-f", key], check=True)
        log("generated new SSH key")
    with open(key + ".pub") as fh:
        log(f"SSH public key (add to Advanced SSH authorized_keys): {fh.read().strip()}")


def self_test():
    ok, out = ssh("ha core info --raw-json >/dev/null && echo ha-cli-ok", timeout=60)
    status = "SSH + ha CLI ok" if ok and "ha-cli-ok" in out else f"SSH test FAILED: {out}"
    mqtt = "MQTT check on" if (HA_TOKEN and CANARIES) else "MQTT check off (no HA_TOKEN/MQTT_CANARIES)"
    mode = "DRY RUN" if DRY_RUN else "auto-restart on"
    notify("HA watchdog started", f"{status}; {mqtt}; {mode}.", priority=0 if ok else 1)


def main():
    if not HA_HOST:
        log("HA_URL is required, e.g. http://homeassistant.local:8123")
        return 2
    os.makedirs(os.path.dirname(cfg("STATE_FILE")), exist_ok=True)
    ensure_key()
    self_test()
    wd = Watchdog()
    while True:
        try:
            wd.step()
        except Exception as exc:  # never let the watchdog itself die
            log(f"watchdog error: {type(exc).__name__}: {exc}")
        with open(cfg("HEARTBEAT_FILE"), "w") as fh:
            fh.write(str(time.time()))
        time.sleep(cfg_int("CHECK_INTERVAL"))


if __name__ == "__main__":
    sys.exit(main())
