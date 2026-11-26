#!/usr/bin/env python3
"""wm-screen-sync-http - ADVANCED, OPTIONAL helper for the WM Device Orca plugin.

Not needed to use the plugin. Without it, a spool colour/material change on a
WonderMaker ZR Ultra / Ultra-S FACTORY printer is written to tmt1.ini and the
touchscreen shows it the next time its own app (client) restarts. With this
helper installed, the plugin can also make the touchscreen update immediately,
with no restart.

What it does: runs as root on the factory screen board (the "TM-T1" board,
also used as the .36/.37 screen boards in this project). Listens on
0.0.0.0:8766.
- POST /refresh re-reads /home/t13dp/printer_data/config/tmt1.ini and pushes
  the four [slot] colour and material values straight into the running
  factory screen app's (client's) memory via /proc/<pid>/mem, so the
  touchscreen redraws without restarting that app.
- GET  /light           reports the chamber light's current state (on/off).
- POST /light?action=on|off|toggle  turns the chamber light on/off/toggle.
  This runs the same /usr/local/sbin/wm-chamber-light script the physical
  "Chamber light" button on the touchscreen already uses every day - this
  helper already runs as root, so it calls the script directly with no sudo
  needed. It never touches any GPIO directly itself.
  After a successful on/off/toggle it also writes a single byte into the
  screen app's own in-memory "is the bulb icon on" flag (the same flag the
  app itself already re-reads from the real GPIO pin every ~10 seconds on
  its own, via get_GPIO2_B4_value()), so the touchscreen's own light icon
  flips immediately instead of waiting out that poll. Same /proc/<pid>/mem
  approach and same safety net as the colour refresh above: it checks the
  byte there still looks like a plain 0/1 flag before writing, and if the
  poke doesn't happen for any reason the icon still self-corrects within a
  few seconds on its own, so a failure here is silently skipped, not an
  error returned to the caller.

Why this is safe to leave installed: the only thing it does is copy numbers
that are already in tmt1.ini into the screen app's own in-memory copy of the
same numbers, or call the same chamber-light script the on-screen button
already uses. It never touches Klipper, never sends G-code, and never writes
anything the plugin (or the touchscreen itself) couldn't already write to
tmt1.ini. Before writing, it checks the existing bytes at the target address
still look like a colour/material pair (0-20 / 0-26); if they don't - for
example on a different client build with a different memory layout - it
refuses to write and reports why, rather than guessing.

Caveats:
- The memory addresses below were found by reverse-engineering one specific
  "client" screen-app build (sha256 b60c96c5caf980a127a3d284e5ac98e799dfd79
  691cb1a52aafd06fd818c4715, Nov 2026). A different WonderMaker firmware
  build may use different addresses; the range/byte checks are the safety
  net for that case, but they are not a guarantee.
- This service listens on your local network with no login of its own -
  anyone who can reach port 8766 on this printer can trigger a refresh or
  toggle the chamber light (never anything more than that). Only install
  this on a network you trust, the same way Fluidd/Moonraker itself has no
  login by default.
- The /light endpoint depends on /usr/local/sbin/wm-chamber-light existing
  on this board (it is part of the main wm-ultra-rpi screen-board install).
  If that script is missing, /light reports the error rather than guessing
  at a GPIO.
- Entirely optional. Uninstall any time with:
    sudo systemctl disable --now wm-screen-sync-http
    sudo rm /usr/local/sbin/wm-screen-sync-http /etc/systemd/system/wm-screen-sync-http.service
    sudo systemctl daemon-reload

Install: see install_screen_sync.sh in this same folder, or the main README.
"""
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import urllib.parse

INI_PATH = "/home/t13dp/printer_data/config/tmt1.ini"
BASE_ADDR = 0x384d078   # all_filament_info[0]; verified 2026-11-25 on the client sha256 above
SLOT_STRIDE = 8         # per slot: colour byte, material byte, nozzle-type byte, pad, nozzle-diameter float
SLOTS = 4
PORT = 8766
CHAMBER_LIGHT_BIN = "/usr/local/sbin/wm-chamber-light"
LED_ON_ADDR = 0x386ce2c  # led_on flag read by update_common_status() every ~50 ticks (~10s) from the real
                         # GPIO2_B4 pin; verified 2026-11-26 on the same client sha256 above


def client_pid():
    out = subprocess.run(["pgrep", "-x", "client"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def read_ini_slots():
    text = open(INI_PATH, encoding="utf-8", errors="replace").read()
    section, color, material = "", {}, {}
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        if section != "slot":
            continue
        mc = re.match(r"^color(\d)\s*=\s*(\d+)", line, re.I)
        if mc:
            color[int(mc.group(1))] = int(mc.group(2))
        mm = re.match(r"^material(\d)\s*=\s*(\d+)", line, re.I)
        if mm:
            material[int(mm.group(1))] = int(mm.group(2))
    return color, material


def apply_slots():
    pid = client_pid()
    if not pid:
        return False, "the factory screen app (client) is not running"
    color, material = read_ini_slots()
    try:
        f = open("/proc/%d/mem" % pid, "r+b", buffering=0)
    except OSError as exc:
        return False, "cannot open /proc/%d/mem: %r" % (pid, exc)
    applied = []
    with f:
        for s in range(SLOTS):
            if s not in color or s not in material:
                continue
            c, m = color[s], material[s]
            if not (0 <= c <= 20 and 0 <= m <= 26):
                continue  # tmt1.ini itself looks wrong for this slot: leave it alone
            off = BASE_ADDR + s * SLOT_STRIDE
            f.seek(off)
            before = f.read(2)
            if len(before) < 2 or before[0] > 20 or before[1] > 26:
                return False, ("slot %d: memory there doesn't look like colour/material data "
                               "(got %r) - refusing to write" % (s + 1, before))
            f.seek(off)
            f.write(bytes([c, m]))
            applied.append(s + 1)
    if not applied:
        return True, "tmt1.ini matched memory already, nothing to change"
    return True, "updated toolhead(s): " + ", ".join(str(s) for s in applied)


def poke_led_icon(state):
    """Best-effort: write the touchscreen's own "is the bulb icon on" flag so it flips the
    moment we change the real light, instead of waiting for its own ~10s GPIO poll to catch up.
    Never raises and never reports failure to the caller - if this doesn't work (wrong client
    build, process not found, unexpected byte there), the icon still self-corrects on its own
    within a few seconds via that same built-in poll."""
    pid = client_pid()
    if not pid:
        return
    want = 1 if state == "on" else 0
    try:
        with open("/proc/%d/mem" % pid, "r+b", buffering=0) as f:
            f.seek(LED_ON_ADDR)
            before = f.read(1)
            if len(before) < 1 or before[0] not in (0, 1):
                return  # doesn't look like the flag we expect on this build: leave it alone
            f.seek(LED_ON_ADDR)
            f.write(bytes([want]))
    except OSError:
        return


def chamber_light(action):
    if action not in ("on", "off", "toggle", "status"):
        return False, "unknown action %r (use on, off, toggle or status)" % action
    if not os.path.exists(CHAMBER_LIGHT_BIN):
        return False, "%s not found on this board" % CHAMBER_LIGHT_BIN
    try:
        proc = subprocess.run([CHAMBER_LIGHT_BIN, action], capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return False, "wm-chamber-light %s: %r" % (action, exc)
    state = proc.stdout.strip()
    if proc.returncode != 0 or state not in ("on", "off"):
        return False, "wm-chamber-light %s failed: %s" % (action, (proc.stderr or state or "no output").strip())
    if action != "status":
        poke_led_icon(state)
    return True, state


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path)
        route = path.path.rstrip("/")
        if route == "/refresh":
            try:
                ok, text = apply_slots()
            except Exception as exc:  # noqa: BLE001
                ok, text = False, "refresh: %r" % exc
            self._send(200 if ok else 500, {"ok": ok, "text": text})
            return
        if route == "/light":
            qs = urllib.parse.parse_qs(path.query)
            action = (qs.get("action", ["toggle"])[0] or "toggle").strip().lower()
            try:
                ok, result = chamber_light(action)
            except Exception as exc:  # noqa: BLE001
                ok, result = False, "light: %r" % exc
            if ok:
                self._send(200, {"ok": True, "state": result})
            else:
                self._send(500, {"ok": False, "text": result})
            return
        self._send(404, {"ok": False, "text": "unknown endpoint"})

    def do_GET(self):
        # Fixed 2026-11-26: must strip the query string like do_POST does, or a GET to
        # "/light?action=status" (used by the Orca plugin's periodic status poll) never
        # matches "/light" below and silently 404s every time.
        route = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if route == "/health":
            self._send(200, {"ok": True, "text": "wm-screen-sync-http is running"})
            return
        if route == "/light":
            try:
                ok, result = chamber_light("status")
            except Exception as exc:  # noqa: BLE001
                ok, result = False, "light: %r" % exc
            if ok:
                self._send(200, {"ok": True, "state": result})
            else:
                self._send(500, {"ok": False, "text": result})
            return
        self._send(404, {"ok": False, "text": "unknown endpoint"})

    def log_message(self, fmt, *args):
        pass  # systemd still captures stdout/stderr for `journalctl -u wm-screen-sync-http`


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("wm-screen-sync-http must run as root (it writes to another process's memory)")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
