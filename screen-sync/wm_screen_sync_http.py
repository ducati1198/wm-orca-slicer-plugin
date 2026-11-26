#!/usr/bin/env python3
"""wm-screen-sync-http - ADVANCED, OPTIONAL helper for the WM Device Orca plugin.

Not needed to use the plugin. Without it, a spool colour/material change on a
WonderMaker ZR Ultra / Ultra-S FACTORY printer is written to tmt1.ini and the
touchscreen shows it the next time its own app (client) restarts. With this
helper installed, the plugin can also make the touchscreen update immediately,
with no restart.

What it does: runs as root on the factory screen board (the "TM-T1" board,
also used as the .36/.37 screen boards in this project). Listens on
0.0.0.0:8766. POST /refresh re-reads
/home/t13dp/printer_data/config/tmt1.ini and pushes the four [slot] colour and
material values straight into the running factory screen app's (client's)
memory via /proc/<pid>/mem, so the touchscreen redraws without restarting
that app.

Why this is safe to leave installed: the only thing it does is copy numbers
that are already in tmt1.ini into the screen app's own in-memory copy of the
same numbers. It never touches Klipper, never sends G-code, and never writes
anything the plugin (or the touchscreen itself) couldn't already write to
tmt1.ini. Before writing, it checks the existing bytes at the target address
still look like a colour/material pair (0-20 / 0-26); if they don't - for
example on a different client build with a different memory layout - it
refuses to write and reports why, rather than guessing.

Caveats:
- The memory address below was found by reverse-engineering one specific
  "client" screen-app build (sha256 b60c96c5caf980a127a3d284e5ac98e799dfd79
  691cb1a52aafd06fd818c4715, Nov 2026). A different WonderMaker firmware
  build may use a different address; the range check above is the safety net
  for that case, but it is not a guarantee.
- This service listens on your local network with no login of its own -
  anyone who can reach port 8766 on this printer can trigger a refresh (never
  anything more than that: it only ever re-applies tmt1.ini). Only install
  this on a network you trust, the same way Fluidd/Moonraker itself has no
  login by default.
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

INI_PATH = "/home/t13dp/printer_data/config/tmt1.ini"
BASE_ADDR = 0x384d078   # all_filament_info[0]; verified 2026-11-25 on the client sha256 above
SLOT_STRIDE = 8         # per slot: colour byte, material byte, nozzle-type byte, pad, nozzle-diameter float
SLOTS = 4
PORT = 8766


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


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") != "/refresh":
            self._send(404, {"ok": False, "text": "unknown endpoint"})
            return
        try:
            ok, text = apply_slots()
        except Exception as exc:  # noqa: BLE001
            ok, text = False, "refresh: %r" % exc
        self._send(200 if ok else 500, {"ok": ok, "text": text})

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True, "text": "wm-screen-sync-http is running"})
        else:
            self._send(404, {"ok": False, "text": "unknown endpoint"})

    def log_message(self, fmt, *args):
        pass  # systemd still captures stdout/stderr for `journalctl -u wm-screen-sync-http`


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("wm-screen-sync-http must run as root (it writes to another process's memory)")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
