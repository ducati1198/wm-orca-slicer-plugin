# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "WM Device"
# description = "Device tab for the WonderMaker Ultra series and ACE Pro printers (Moonraker): live status, temperature trends, toolheads with colour and material, position, camera, grouped messages, file list, print history, pause/resume/cancel and start print. The plugin talks to the printer; the tab only shows what it receives. On factory printers (the stock touchscreen board), the chamber light button is always shown, and a spool colour/material change saved here normally only shows up on the touchscreen after its app restarts. Controlling the light and the instant screen/colour updates need the optional wm-screen-sync-http helper - without it, the light button just explains how to install it, and the touchscreen catches up on its own next app restart. If you install that optional helper on the printer over SSH (one-time command, see the README's 'Factory printers: live screen refresh' section), the touchscreen updates immediately after a colour/material change and after a light toggle, and the light button itself turns green while the light is on. On a factory printer without the helper, the Device tab now also shows an 'Install helper on printer' button (macOS/Linux builds of Orca only) that runs the same one-line SSH install for you, after you confirm a warning popup; the username and password fields are pre-filled with the factory default login and can be edited."
# author = "ducati1198"
# version = "0.29.17"
# ///
"""WM Device v0.29.17.

Reads the printer with GET requests every 2 s. The only things it ever WRITES are the buttons you press:
Pause, Resume, Cancel (asks first), Print a file (asks first), and the Controls card: set a temperature,
fan or speed, home, cool down. Every control asks first, and the plugin builds the G-code itself from checked
numbers (the page can never send free G-code). Each is one Moonraker POST request.
"""
import json
import os
import re
import threading
import webbrowser
import time
import urllib.parse
import urllib.request

import orca

POLL_S = 2.0
TIMEOUT_S = 3.0
HEADS = 4
EXTRUDERS = ["extruder", "extruder1", "extruder2", "extruder3"]
WANT = ["print_stats", "display_status", "virtual_sdcard", "heater_bed", "heater_generic chamber", "toolhead",
        "fan", "fan_generic auxiliary_fan", "fan_generic air_fan", "idle_timeout", "gcode_move", "webhooks",
        "ace", "save_variables", "output_pin ACE_Pro", "output_pin ACE_Auto_Load", "gcode_macro _WM_ACE",
        "gcode_macro _WM_ENDLESS", "gcode_macro _WM_ENDLESS_CONTINUE", "gcode_macro WM_CHAMBER_LIGHT", "gcode_macro WM_UNLOAD_MENU",
        "gcode_macro _ZR_SET_TOOL_INFO"] + [f"ace_instance_{i}" for i in range(4)] + EXTRUDERS + [f"filament_switch_sensor {n}{i}" for n in ("filament", "docked", "path_") for i in range(HEADS)]

# Same lists as the ZR Tools screen / Spool and Toolhead Mapper plugin
ZR_COLOURS = ["#FFFFFF", "#F3E8C8", "#9A6738", "#C7D5DF", "#000000", "#00DDE3", "#13A7E8", "#0B4BD8", "#16E900",
              "#00A84F", "#FFF200", "#FF5A1F", "#DDBFDE", "#E72DBE", "#FF1C2E", "#006EFF", "#D9A520", "#B86B44",
              "#AFC0CC", "#C060C0", "#D5E8EE"]
ZR_MATERIALS = ["PLA", "PLA Silk", "ABS", "PETG", "PLA Matte", "PLA Metal", "ABS Matte", "PETG Matte", "PVA", "TPE",
                "Marble", "HIPS", "TPU", "PET", "Wood", "ASA", "PLA+", "PLA-CF", "PETG-CF", "PA", "PA-CF", "PC",
                "ABS-CF", "PA-GF", "PC-ABS", "PP", "BVOH"]


# Same lists as the Pi's own macros (rpi_base.cfg WM_UNLOAD_MENU) and ZR_SET_TOOL_INFO (zr_tools.cfg): MAT 0 = empty
SPOOL_MATS = {1: "PLA", 2: "PLA+", 3: "PLA Silk", 4: "PLA Matte", 5: "PLA-CF", 6: "PETG", 7: "PETG-CF", 8: "ABS", 9: "ASA",
              10: "TPU", 11: "PA", 12: "PC", 13: "PVA", 14: "HIPS", 15: "PLA Metal"}
SPOOL_COLOURS = [("White", "FFFFFF"), ("Beige", "F3E8C8"), ("Brown", "9A6738"), ("Grey", "C7D5DF"), ("Black", "000000"),
                 ("Cyan", "00DDE3"), ("Light blue", "13A7E8"), ("Dark blue", "0B4BD8"), ("Light green", "16E900"),
                 ("Dark green", "00A84F"), ("Yellow", "FFF200"), ("Orange", "FF5A1F"), ("Light pink", "DDBFDE"),
                 ("Magenta", "E72DBE"), ("Red", "FF1C2E")]


def merge_saved(slots, status):
    """Spool info saved on the Pi (zr_tN_color / zr_tN_mat / zr_tN_temp by ZR_SET_TOOL_INFO) wins over the screen's tmt1.ini."""
    v = ((status or {}).get("save_variables") or {}).get("variables") or {}
    out = []
    for i in range(HEADS):
        k = dict(slots[i]) if i < len(slots) else {"colour": "", "material": ""}
        try:
            c = int(v.get("zr_t%d_color" % i, 0) or 0)
            if c > 0:
                k["colour"] = "#%06X" % (c & 0xFFFFFF)
            m = int(v.get("zr_t%d_mat" % i, 0) or 0)
            if m in SPOOL_MATS:
                mi = k.get("midx")
                if not (isinstance(mi, int) and 0 <= mi < len(TOUCH_MATS) and TOUCH_MATS[mi] == m):
                    mi = next((n for n, z in enumerate(TOUCH_MATS) if z == m), None)
                    k["material"] = SPOOL_MATS[m]
                k["midx"] = mi          # exact entry of the touchscreen list (0-26), so PA-CF stays PA-CF
            k["mat"] = m
            k["temp"] = int(v.get("zr_t%d_temp" % i, 0) or 0)
        except (TypeError, ValueError):
            pass
        out.append(k)
    return out


def base_url(text):
    t = (text or "").strip().replace("http://", "").replace("https://", "").rstrip("/")
    if not t:
        return ""
    return "http://" + (t if ":" in t else t + ":7125")


def http_json(url, post=False, timeout=None, method=None):
    req = urllib.request.Request(url, data=b"" if post else None, method="POST" if post else (method or "GET"),
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def http_text(url):
    with urllib.request.urlopen(url, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


# The touchscreen and Fluidd keep each toolhead's colour and material in config/tmt1.ini as list numbers.
TOUCH_PALETTE = [
    ("White", 0xFFFFFF), ("Beige", 0xF3E8C8), ("Brown", 0x9A6738), ("Grey", 0xC7D5DF), ("Black", 0x000000),
    ("Cyan", 0x00DDE3), ("Light blue", 0x13A7E8), ("Dark blue", 0x0B4BD8), ("Light green", 0x16E900),
    ("Dark green", 0x00A84F), ("Yellow", 0xFFF200), ("Orange", 0xFF5A1F), ("Light pink", 0xDDBFDE),
    ("Magenta", 0xE72DBE), ("Red", 0xFF1C2E), ("Bright blue", 0x006EFF), ("Gold", 0xD9A520),
    ("Copper", 0xB86B44), ("Silver", 0xAFC0CC), ("Multicolour", 0xC060C0), ("Clear", 0xD5E8EE),
]
# tmt1.ini material number -> ZR material number (same order as the touchscreen list)
TOUCH_MATS = [1, 3, 8, 6, 4, 15, 8, 6, 13, 10, 15, 14, 10, 15, 15, 9, 2, 5, 7, 11, 11, 12, 8, 11, 12, 15, 10]


def nearest_palette(c):
    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
    best, dist = 0, None
    for i, (_n, pc) in enumerate(TOUCH_PALETTE):
        cr, cg, cb = (pc >> 16) & 255, (pc >> 8) & 255, pc & 255
        rm = (r + cr) / 2.0
        d = (2 + rm / 256) * (r - cr) ** 2 + 4 * (g - cg) ** 2 + (2 + (255 - rm) / 256) * (b - cb) ** 2
        if dist is None or d < dist:
            best, dist = i, d
    return best


def update_tmt1(text, index, color, material):
    """tmt1.ini text with one slot changed; every other line stays as it was."""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    section, slot_end, dc, dm = "", -1, False, False
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            if section == "slot":
                slot_end = i
            section = m.group(1).strip().lower()
            continue
        if section != "slot":
            continue
        if re.match(r"^color%d\s*=" % index, line, re.I):
            lines[i], dc = "color%d=%d" % (index, color), True
        elif re.match(r"^material%d\s*=" % index, line, re.I):
            lines[i], dm = "material%d=%d" % (index, material), True
    if not (dc and dm):
        extra = []
        if not dc:
            extra.append("color%d=%d" % (index, color))
        if not dm:
            extra.append("material%d=%d" % (index, material))
        if section == "slot" and slot_end < 0:
            at = len(lines)
        elif slot_end >= 0:
            at = slot_end
        else:
            lines += ["", "[slot]"]
            at = len(lines)
        while at > 0 and lines[at - 1].strip() == "":
            at -= 1
        lines[at:at] = extra
    return eol.join(lines) + (eol if text.endswith(("\n", "\r")) else "")


def refresh_screen_live(base, timeout=2):
    """Best-effort call to the OPTIONAL wm-screen-sync-http helper (see README,
    screen-sync/). Returns True only if it applied the refresh; False if it
    isn't installed, isn't reachable, or failed - the caller should fall back
    to the normal "restarts its app" message in that case."""
    try:
        host = urllib.parse.urlsplit(base).hostname
        if not host:
            return False
        req = urllib.request.Request("http://%s:8766/refresh" % host, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def chamber_light_live(base, action, timeout=3):
    """Best-effort call to the OPTIONAL wm-screen-sync-http helper's /light endpoint
    (same helper as refresh_screen_live, see README, screen-sync/). On a factory
    touchscreen board this runs the same wm-chamber-light script the on-screen
    "Chamber light" button already uses, so it is no riskier than that button.
    action is "on", "off", "toggle" or "status". Returns the resulting state
    ("on"/"off") on success, or None if the helper isn't installed, isn't
    reachable, or failed - the caller should fall back to telling the person to
    use the printer's own switch in that case."""
    try:
        host = urllib.parse.urlsplit(base).hostname
        if not host:
            return None
        url = "http://%s:8766/light?action=%s" % (host, urllib.parse.quote(action, safe=""))
        req = urllib.request.Request(url, data=(b"" if action != "status" else None),
                                      method="POST" if action != "status" else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("state") in ("on", "off"):
                return data["state"]
            return None
    except Exception:
        return None


# One-click installer for the OPTIONAL wm-screen-sync-http helper (README 2.4b). macOS/Linux only
# for now (Windows falls back to the manual SSH/README instructions).
HELPER_INSTALL_CMD = (
    "curl -fsSL https://raw.githubusercontent.com/ducati1198/wm-orca-slicer-plugin/main/"
    "screen-sync/install_screen_sync.sh | sudo bash"
)
_WM_SSH_OK = "WM_SSH_INSTALL_OK"
_WM_SSH_FAIL = "WM_SSH_INSTALL_FAIL"


def ssh_run_with_password(host, user, password, command, timeout=25):
    """Runs one command on `host` over SSH, authenticating with `password` (no sshpass, no
    paramiko - pure stdlib). Used only by the Device tab's "Install helper on printer" button, and
    only after the person has confirmed it and typed (or kept the pre-filled factory default)
    credentials; see on_message()'s "install_helper" handler and the README's 2.4b.

    Deliberately does NOT fork this process (no os.fork(), no pty, no preexec_fn). An earlier
    version used fork()+setsid()+TIOCSCTTY+execvp to drive a real controlling-terminal SSH session,
    which crashed OrcaSlicer outright on macOS with a CoreFoundation fork-safety abort
    ("...YOU_CANNOT_USE_THIS_COREFOUNDATION_FUNCTIONALITY...YOU_MUST_EXEC...") - confirmed by a
    battery of isolated tests: fork alone, setsid alone, the TIOCSCTTY ioctl alone and the dup2
    redirects alone are all individually safe, but that same sequence immediately followed by
    execvp (to launch the real `ssh` binary) reliably takes the whole host app down, not just the
    child. Setting OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES did not help (confirmed by real-world
    retest - the guard fires at a point this runtime env var can no longer reach).

    Instead this uses OpenSSH's SSH_ASKPASS_REQUIRE=force (OpenSSH >= 8.4, present on current
    macOS/Linux builds) so ssh itself invokes a tiny disposable one-line helper script for the
    password, instead of needing a controlling terminal at all. The whole thing runs through a
    plain subprocess.run() call - no fork(), no preexec_fn, no pty - which CPython implements via
    posix_spawn() on macOS/Linux, a code path the CoreFoundation fork-safety guard does not
    instrument. Verified end-to-end against a real printer with no crash.

    The disposable askpass script is written inside this plugin's own _own_dir() (same folder
    hosts.json already lives in), NOT via tempfile.mkstemp()/the system temp dir. Orca audits
    plugin file operations per-plugin: tempfile.mkstemp() was denied outright for this plugin
    ("Plugin attempted audited operation 'tempfile.mkstemp' without permission") even though the
    exact same call was allowed inside a different, throwaway test plugin - Orca's own dialog said
    to "use an allowed location", and _own_dir() is the one this plugin already writes to
    successfully today. Returns (ok, output_text).
    """
    import stat
    import subprocess
    askpass_dir = _own_dir()
    os.makedirs(askpass_dir, exist_ok=True)
    askpass_path = os.path.join(askpass_dir, "wm_askpass_%d_%d.sh" % (os.getpid(), int(time.time() * 1000)))
    try:
        with open(askpass_path, "w") as f:
            f.write("#!/bin/sh\necho \"%s\"\n" % password.replace("\\", "\\\\").replace('"', '\\"'))
        os.chmod(askpass_path, stat.S_IRWXU)
        env = dict(os.environ)
        env["SSH_ASKPASS"] = askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.pop("DISPLAY", None)
        argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
                "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                "%s@%s" % (user, host), command]
        try:
            proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            partial = (exc.output or b"").decode(errors="replace") if exc.output else ""
            return False, "timed out reaching the printer over SSH:\n" + partial
        out = proc.stdout.decode(errors="replace")
        return proc.returncode == 0, out
    finally:
        try:
            os.remove(askpass_path)
        except OSError:
            pass


def install_screen_sync_helper(base, user, password):
    """Installs the optional wm-screen-sync-http helper on the printer's screen board over SSH -
    the same one-liner the README's 2.4b has the person run by hand, just typed for them after they
    confirm the warning popup. `base` is the Moonraker base URL already in use for this printer
    (same host, different port), so no separate IP entry is needed."""
    host = urllib.parse.urlsplit(base).hostname
    if not host:
        return False, "no printer host known yet - open the Device tab and let it connect first"
    if os.name == "nt":
        return False, ("one-click install isn't available on Windows yet - see the README's "
                        "2.4b section for the manual SSH steps")
    # the command's own success/fail sentinel is what we trust, not ssh's process exit code alone
    # (belt and suspenders: a flaky exit-status path should never show "Installed" on a failure)
    wrapped = "(" + HELPER_INSTALL_CMD + ") && echo " + _WM_SSH_OK + " || echo " + _WM_SSH_FAIL
    try:
        exited_ok, out = ssh_run_with_password(host, user, password, wrapped)
    except Exception as exc:  # noqa: BLE001
        return False, "install failed: %r" % exc
    tail = "\n".join(out.strip().splitlines()[-12:])
    if "assword" in out and "Permission denied" in out:
        return False, "wrong username or password for this printer"
    if ("Could not resolve hostname" in out or "No route to host" in out
            or "Connection refused" in out or "Connection timed out" in out):
        return False, "could not reach the printer over SSH:\n" + tail
    if _WM_SSH_OK in out and exited_ok:
        return True, "Installed. " + tail
    return False, "install did not finish cleanly:\n" + tail


def upload_config(base, subdir, name, text):
    """Moonraker file upload into the config root (same call the Fluidd panel uses)."""
    import uuid
    b = uuid.uuid4().hex
    parts = f'--{b}\r\nContent-Disposition: form-data; name="root"\r\n\r\nconfig\r\n'
    if subdir:
        parts += f'--{b}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{subdir}\r\n'
    body = (parts + f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f'Content-Type: text/plain\r\n\r\n').encode() + text.encode() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(base + "/server/files/upload", body, {"Content-Type": "multipart/form-data; boundary=" + b})
    urllib.request.urlopen(req, timeout=10).read()


def parse_slots(text):
    """tmt1.ini [slot] colorN / materialN -> [{'colour': '#RRGGBB'|'', 'material': ''}] x 4."""
    out = [{"colour": "", "material": ""} for _ in range(HEADS)]
    section = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        sec = re.match(r"^\[(.+)\]$", line)
        if sec:
            section = sec.group(1).strip().lower()
            continue
        if section != "slot":
            continue
        m = re.match(r"^(color|material)([0-3])\s*=\s*(-?\d+)\s*$", line, re.IGNORECASE)
        if m:
            key, head, val = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            if key == "color" and 0 <= val < len(ZR_COLOURS):
                out[head]["colour"] = ZR_COLOURS[val]
            if key == "material" and 0 <= val < len(ZR_MATERIALS):
                out[head]["material"] = ZR_MATERIALS[val]
                out[head]["midx"] = val
    return out


MESSAGE_RE = re.compile(r"error|!!|shutdown|ZRU|timeout|lost communication|warning", re.IGNORECASE)


def orca_print_host():
    """Printer address from Orca's own printer settings (Connection > host), as http://host:7125, or ''."""
    try:
        return base_url(str(orca.host.preset_bundle().full_config_value("print_host") or ""))
    except Exception:  # noqa: BLE001
        return ""


def _host_of(base):
    return str(base or "").replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]


def _own_dir():
    """plugin_data/wm_device found from this file's own path. This is the folder that is readable while the plugin
    loads, so the printer list must always be kept here (a Cloud install can have a different storage() folder)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "plugin_data", "wm_device")


DIAG = {}


def _data_dirs():
    """Folders where the plugin keeps its small files: its own plugin_data folder and Orca's storage() folder."""
    out = [_own_dir()]
    try:
        alt = str(orca.host.plugin.storage())
        if alt and alt not in out:
            out.append(alt)
    except Exception as exc:  # noqa: BLE001
        DIAG["storage"] = repr(exc)[:80]
    return out


def _hosts_files():
    """Every folder that may hold hosts.json: this file's own plugin_data first, then Orca's storage() when it works."""
    out = [os.path.join(_own_dir(), "hosts.json")]
    try:
        alt = os.path.join(str(orca.host.plugin.storage()), "hosts.json")
        if alt not in out:
            out.append(alt)
    except Exception:  # noqa: BLE001
        pass
    return out


def _hosts_file():
    return _hosts_files()[0]


def _read_hosts_from(fp):
    try:
        with open(fp, "r", encoding="utf-8") as fh:
            return [h for h in json.load(fh) if isinstance(h, str) and h]
    except Exception as exc:  # noqa: BLE001
        PERM["err"] = repr(exc)[:160]
        return []


def _write_hosts(hosts):
    """Save the list in every folder that can be written, so the load-time read always finds it."""
    done = False
    bad = []
    for fp in _hosts_files():
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as fh:
                json.dump(hosts[-12:], fh)
            done = True
        except Exception as exc:  # noqa: BLE001
            bad.append(repr(exc)[:60])
    DIAG["hosts_write"] = ("ok" if done else "FAILED") + ((" err " + "; ".join(bad)) if bad else "")
    return done


def known_hosts():
    """Printer hosts this plugin has connected to before (union of every saved copy)."""
    out = []
    for fp in _hosts_files():
        for h in _read_hosts_from(fp):
            if h not in out:
                out.append(h)
    return out


def _trace(msg):
    """Breadcrumbs of the slicing step in plugin_data/wm_device/trace.log (to see where Orca stops if it hangs). Never raises."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "plugin_data", "wm_device")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, "trace.log")
        try:
            if os.path.getsize(fp) > 60000:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    keep = fh.read()[-20000:]
                keep = keep[keep.find("\n") + 1:]
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(keep)
        except OSError:
            pass
        with open(fp, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


def _mem_file():
    # Found from this file's own path: orca.host.plugin.storage() must not be called from the slicing step (it can freeze Orca).
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "plugin_data", "wm_device", "map_memory.json")


def mem_key(name, nfil):
    """Project key: the file name without the time estimate Orca adds (OrcaCube_PLA_47m48s.gcode -> orcacube_pla) + filament count."""
    stem = os.path.basename(str(name or ""))
    for ext in (".3mf", ".gcode", ".bgcode"):
        while stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
    # drop the time estimate wherever it sits (47m49s, 1h20m5s, 2d3h ...), so the key stays the same when the estimate changes
    stem = re.sub(r"[_ -]*(?<![A-Za-z0-9])(?=\d+[dhms])(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?![A-Za-z0-9])", "", stem)
    stem = re.sub(r"[_ -]{2,}", "_", stem).strip("_ -").lower()
    return "%s|%d" % (stem or "project", int(nfil))


def mem_load(key):
    try:
        with open(_mem_file(), "r", encoding="utf-8") as fh:
            v = json.load(fh).get(key)
        return clean_map(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def mem_save(key, mapping):
    try:
        path = _mem_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            data = {}
        data[key] = list(mapping)
        if len(data) > 200:
            data = dict(list(data.items())[-200:])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:  # noqa: BLE001
        pass


def _forgot_file():
    return os.path.join(_own_dir(), "forgotten.json")


def forgotten_hosts():
    try:
        with open(_forgot_file(), "r", encoding="utf-8") as fh:
            return [h for h in json.load(fh) if isinstance(h, str) and h]
    except Exception:  # noqa: BLE001
        return []


def _save_forgotten(hosts):
    try:
        os.makedirs(os.path.dirname(_forgot_file()), exist_ok=True)
        with open(_forgot_file(), "w", encoding="utf-8") as fh:
            json.dump(hosts[-30:], fh)
    except Exception:  # noqa: BLE001
        pass


def set_last_host(host):
    """The printer the tab connected to last, kept in files (the page's own browser storage is not kept on every system)."""
    ok, bad = 0, []
    for d in _data_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "last_host.json"), "w", encoding="utf-8") as fh:
                json.dump(host or "", fh)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            bad.append(repr(exc)[:60])
    DIAG["last_write"] = "ok %d" % ok + ((" err " + "; ".join(bad)) if bad else "")


def get_last_host():
    for d in _data_dirs():
        try:
            with open(os.path.join(d, "last_host.json"), "r", encoding="utf-8") as fh:
                v = json.load(fh)
            if isinstance(v, str) and v:
                return v
        except Exception:  # noqa: BLE001
            pass
    return ""


def remember_host(base, auto=False):
    """Remember a printer host so the next start pre-approves it in the single permission dialog.
    auto=True is for addresses the plugin found by itself (Orca's printer setting): never for one the user erased."""
    host = _host_of(base)
    if not host:
        return
    if auto and host in forgotten_hosts():
        return
    if not auto and host in forgotten_hosts():
        _save_forgotten([h for h in forgotten_hosts() if h != host])
    try:
        hosts = known_hosts()
        if host not in hosts:
            hosts.append(host)
            _write_hosts(hosts)
    except Exception:  # noqa: BLE001
        pass


def forget_host(base):
    """Erase a printer host from the saved list (it is no longer pre-approved at the next install)."""
    host = _host_of(base)
    try:
        hosts = [h for h in known_hosts() if h != host]
        _write_hosts(hosts)
        _save_forgotten([h for h in forgotten_hosts() if h != host] + [host])
    except Exception:  # noqa: BLE001
        pass


PERM = {"ok": False, "hosts": set(), "err": "", "req": ""}


def declare_permissions():
    """The ONE permission request. Orca only accepts it inside register_capabilities() and shows a single combined
    dialog for it; anything not declared here prompts again on first use. Orca forgets granted permissions on every
    plugin update, so this single dialog is all the user sees per new version."""
    request = getattr(orca, "request_permissions", None)
    if request is None:
        return
    hosts = []
    # NEVER read the printer preset here: Orca loads plugins before its preset bundle exists and crashes (segfault in
    # PresetBundle::full_config). The Orca printer host is saved later, from the tab or the slicing step.
    for h in known_hosts():
        if h and h not in hosts:
            hosts.append(h)
    # A bare host covers every port and path on it. Any other form is covered for that exact URL only (one popup per URL).
    try:
        request(network_http=["http://" + h for h in hosts], threading=True)
        PERM["ok"] = True
        PERM["hosts"] = set(hosts)
        PERM["req"] = "requested " + ", ".join(hosts)
        _trace("permissions requested for [%s] from %s" % (", ".join(hosts), os.path.abspath(__file__)))
    except Exception as exc:  # noqa: BLE001
        PERM["req"] = "request failed: " + repr(exc)[:160]
        _trace("permission request failed: " + repr(exc)[:160])
        try:
            request(threading=True)
        except Exception:  # noqa: BLE001
            pass


def _diag_text():
    """Where the saved printer list lives and what is in it (shown small in the tab, to find why it is not kept)."""
    try:
        lines = []
        for fp in _hosts_files():
            try:
                sz = os.path.getsize(fp)
                with open(fp, "r", encoding="utf-8") as fh:
                    n = len(json.load(fh))
                lines.append("FILE %s: %d bytes, %d printers" % (fp, sz, n))
            except Exception as exc:  # noqa: BLE001
                lines.append("FILE %s: %s" % (fp, repr(exc)[:70]))
        lines.append("last=%r" % get_last_host())
        lines.append("; ".join("%s=%s" % kv for kv in DIAG.items()))
        return "\n".join(lines)[:700]
    except Exception as exc:  # noqa: BLE001
        return "diag error " + repr(exc)[:100]


class Poller:
    """Background thread: reads Moonraker every POLL_S seconds and hands the result to `post`."""

    def __init__(self, post):
        self.post = post
        self.base = ""
        self.stop = threading.Event()
        self.thread = None
        self.objects = None
        self.cams = []
        self.slots = [{"colour": "", "material": ""} for _ in range(HEADS)]
        self.has_tmt1 = False
        self.light_state = None   # "on" / "off" / None (unknown - no helper, or a real Klipper macro printer)
        self.helper_ok = False   # True once the optional wm-screen-sync-http helper answers /light?action=status
        self.messages = []
        self.tick = 0
        self.print_state = ""
        self.fails = 0
        self.kstate = ""
        self.last = {}
        self.pseen = None
        self.pr = None
        self.pgrp = 0
        self.pallow = set()
        self.ping = 0.0

    def shutdown(self):
        """Stop polling for good (plugin unloaded / deactivated). Without this the old thread kept asking Orca for
        network permission after an update, which produced the stack of permission pop-ups."""
        self.stop.set()
        self.base = ""

    def beat(self):
        self.ping = time.time()
        if self.base and (self.thread is None or not self.thread.is_alive()):
            self.stop.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def set_base(self, base):
        if base != self.base:
            self.base = base
            self.objects = None
            self.tick = 0
            self.has_tmt1 = False
            self.light_state = None
            self.helper_ok = False
        self.ping = time.time()
        if base and (self.thread is None or not self.thread.is_alive()):
            self.stop.clear()
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def run(self):
        while not self.stop.is_set():
            if self.ping and time.time() - self.ping > 15:   # the page stopped sending heartbeats: it is gone
                return
            base = self.base
            try:
                if not base:
                    time.sleep(POLL_S)
                    continue
                if self.objects is None or self.tick % 20 == 19 or (self.objects and "extruder" not in self.objects):
                    # re-read the object list now and then: if it was read while Klipper was restarting it misses the toolheads
                    listing = http_json(base + "/printer/objects/list")["result"]["objects"]
                    self.objects = [o for o in WANT if o in listing]
                    try:
                        self.cams = http_json(base + "/server/webcams/list")["result"]["webcams"]
                    except Exception:  # noqa: BLE001
                        self.cams = []
                if self.tick % 5 == 0:   # slow data: filament colours/materials and the console messages
                    try:
                        self.slots = parse_slots(http_text(base + "/server/files/config/tmt1.ini"))
                        self.has_tmt1 = True   # this printer has a factory screen board (tmt1.ini exists)
                    except Exception:  # noqa: BLE001
                        pass
                    if self.has_tmt1 and "gcode_macro WM_CHAMBER_LIGHT" not in (self.objects or []):
                        # factory printer with no Klipper light macro: ask the optional live helper
                        # for the real state, so the Light button can show it as on/off.
                        st = chamber_light_live(base, "status", timeout=1.5)
                        self.helper_ok = st is not None
                        if st is not None:
                            self.light_state = st
                    try:
                        store = http_json(base + "/server/gcode_store?count=60")["result"]["gcode_store"]
                        self.messages = [{"t": m.get("time"), "text": m.get("message", "")[:240]}
                                         for m in store if MESSAGE_RE.search(m.get("message", ""))][-8:]
                    except Exception:  # noqa: BLE001
                        pass
                self.tick += 1
                q = "&".join(urllib.parse.quote(o, safe="") for o in self.objects)
                status = http_json(base + "/printer/objects/query?" + q)["result"]["status"]
                self.last = status
                self.print_state = (status.get("print_stats") or {}).get("state", "")
                self.fails = 0
                self.kstate = (status.get("webhooks") or {}).get("state", "")
                self.post({"type": "state", "base": base, "status": status, "time": time.time(), "cams": self.cams,
                           "slots": merge_saved(self.slots, status), "messages": self.messages, "hasTmt1": self.has_tmt1,
                           "lightOn": self.light_state, "helperOk": self.helper_ok})
                try:
                    self.scan_prompts(base)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                self.objects = None
                self.fails += 1
                self.post({"type": "error", "base": base, "text": repr(exc)})
            self.stop.wait(POLL_S)

    # ---- things the user asks for (each runs in its own short thread) ----
    def files(self):
        try:
            rows = http_json(self.base + "/server/files/list?root=gcodes")["result"]
            rows = sorted(rows, key=lambda r: r.get("modified", 0), reverse=True)[:25]
            self.post({"type": "files", "files": [{"path": r["path"], "size": r.get("size", 0),
                                                   "modified": r.get("modified", 0)} for r in rows]})
            threading.Thread(target=self.file_meta, daemon=True, args=([r["path"] for r in rows],)).start()
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "result", "ok": False, "text": "file list: " + repr(exc)})

    def scan_prompts(self, base):
        """Printer prompt windows (RESPOND action:prompt_*) come through the console; the page draws them like Fluidd does."""
        store = http_json(base + "/server/gcode_store?count=40", timeout=4)["result"]["gcode_store"]
        keys = [(m.get("time"), m.get("message", "")) for m in store]
        if self.pseen is None:
            self.pseen = set(keys)      # do not replay old prompts
            return
        new = [k for k in keys if k not in self.pseen]
        self.pseen = set(keys)
        for _t, msg in new:
            m = re.search(r"action:prompt_(\w+)\s*(.*)$", msg)
            if not m:
                continue
            cmd, arg = m.group(1), m.group(2).strip()
            if cmd == "begin":
                self.pr, self.pgrp = {"title": arg[:80], "items": []}, 0
            elif self.pr is None and cmd != "end":
                continue
            elif cmd == "text":
                self.pr["items"].append({"t": "text", "text": arg[:400]})
            elif cmd == "button_group_start":
                self.pgrp += 1
            elif cmd == "button_group_end":
                self.pgrp += 1
            elif cmd in ("button", "footer_button"):
                parts = arg.split("|")
                label = parts[0][:60]
                script = parts[1].strip() if len(parts) > 1 else ""
                color = parts[2].strip() if len(parts) > 2 else "secondary"
                self.pr["items"].append({"t": "btn", "label": label, "script": script, "color": color,
                                         "grp": self.pgrp if cmd == "button" else -1})
            elif cmd == "show":
                self.pallow = set(i["script"] for i in self.pr["items"] if i["t"] == "btn" and i["script"])
                self.post({"type": "prompt", "prompt": self.pr})
            elif cmd == "end":
                self.pr, self.pallow = None, set()
                self.post({"type": "prompt", "close": True})

    def prompt_click(self, script):
        script = str(script or "")
        self.post({"type": "prompt", "close": True})
        if not script:
            return
        if script not in self.pallow:
            self.post({"type": "result", "ok": False, "text": "that button is not from the printer's window"})
            return
        try:
            http_json(self.base + "/printer/gcode/script?script=" + urllib.parse.quote(script, safe=""), post=True, timeout=240)
            self.post({"type": "result", "ok": True, "text": "sent: " + script[:60]})
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "result", "ok": False, "text": script[:40] + ": " + repr(exc)[:120]})

    def delete_files(self, paths):
        ok, errs = 0, []
        for p in paths:
            p = str(p or "").strip().lstrip("/")
            if not p or ".." in p.split("/") or "\\" in p:
                errs.append("skipped a bad file name")
                continue
            try:
                http_json(self.base + "/server/files/gcodes/" + urllib.parse.quote(p, safe="/"), method="DELETE", timeout=15)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errs.append(p + ": " + repr(exc)[:90])
        text = "deleted %d file%s" % (ok, "" if ok == 1 else "s")
        if errs:
            text += "; not deleted: " + "; ".join(errs[:3])
        self.post({"type": "result", "ok": not errs, "text": text})
        self.files()

    def file_meta(self, paths):
        """Part picture, filament colours and weight of each listed file (read only, from Moonraker's file data)."""
        base, out = self.base, {}
        for n, path in enumerate(paths):
            try:
                md = http_json(base + "/server/files/metadata?filename=" + urllib.parse.quote(path, safe=""), timeout=6)["result"]
                th, best = "", 0
                for t in md.get("thumbnails") or []:
                    w = int(t.get("width") or 0)
                    if best < w <= 300 and t.get("relative_path"):
                        best = w
                        folder = os.path.dirname(path)
                        th = base + "/server/files/gcodes/" + urllib.parse.quote((folder + "/" if folder else "") + t["relative_path"], safe="/")
                cols = [str(c) for c in (md.get("filament_colors") or md.get("extruder_colors") or [])][:HEADS]
                wts = md.get("filament_weights") or []
                out[path] = {"c": cols, "u": [(float(wts[i]) > 0) if i < len(wts) else True for i in range(len(cols))],
                             "g": md.get("filament_weight_total") or 0, "h": md.get("estimated_time") or 0, "th": th}
            except Exception:  # noqa: BLE001
                pass
            if base != self.base:
                return
            if out and (n % 5 == 4 or n == len(paths) - 1):
                self.post({"type": "filemeta", "meta": out})
                out = {}

    def print_with_map(self, filename):
        """Print from the file list: open the tool mapping window for the filaments of that file, write the map, start the print."""
        try:
            if self.print_state not in ("standby", "complete", "cancelled", "error"):
                self.post({"type": "result", "ok": False, "text": f"start: not possible while the printer is '{self.print_state}'"})
                return
            if not filename or ".." in filename:
                raise ValueError("no file name")
            md = http_json(self.base + "/server/files/metadata?filename=" + urllib.parse.quote(filename, safe=""), timeout=8)["result"]

            def as_list(v):
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except Exception:  # noqa: BLE001
                        v = [v]
                return list(v) if isinstance(v, (list, tuple)) else []

            cols = as_list(md.get("filament_colors") or md.get("extruder_colors"))
            types, names, wts = as_list(md.get("filament_type")), as_list(md.get("filament_name")), as_list(md.get("filament_weights"))
            n = min(len(cols), HEADS)
            if n == 0:
                self.post({"type": "result", "ok": True, "text": "no filament information in this file: printing without a tool map"})
                self.command("start", filename)
                return
            fils = []
            for i in range(n):
                nm = str(names[i]) if i < len(names) else ""
                if i < len(wts) and float(wts[i]) <= 0:
                    nm = (nm + " (not used)").strip()
                fils.append({"index": i, "colour": str(cols[i]), "type": str(types[i]) if i < len(types) else "", "name": nm})
            host = self.base.replace("http://", "").replace(":7125", "")
            key = mem_key(filename, n)
            note = ""
            while True:
                heads, cur, _ = fetch_heads(self.base)
                page = file_popup_html(fils, heads, cur, filename, host, note, mem_load(key))
                self.post({"type": "result", "ok": True, "text": "mapping window opened for " + os.path.basename(filename)})
                res = ui_dialog(orca.host.ui, page, "WM Device: print with tool mapping", 860, 720)
                act = (res or {}).get("action")
                if act == "sync":
                    note = "Synced from the printer"
                    continue
                break
            if act == "skip":
                self.command("start", filename)
                return
            if act != "print":
                self.post({"type": "result", "ok": False, "text": "print cancelled: nothing was sent"})
                return
            got = clean_map((res or {}).get("map"))
            if got:
                mem_save(key, got)
                self.action({"kind": "toolmap", "map": got})
            self.command("start", filename)
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "result", "ok": False, "text": "print with map: " + repr(exc)})

    def timelapse(self):
        """List the timelapse videos (Moonraker root 'timelapse'); the preview picture is the .jpg with the same name."""
        try:
            rows = http_json(self.base + "/server/files/list?root=timelapse")["result"]
            pics = {}
            for r in rows:
                p = r.get("path", "")
                if p.lower().endswith(".jpg"):
                    pics[os.path.splitext(p)[0].replace("_previewImage", "")] = p
            vids = []
            for r in sorted(rows, key=lambda r: r.get("modified", 0), reverse=True):
                p = r.get("path", "")
                if not p.lower().endswith((".mp4", ".webm")):
                    continue
                pic = pics.get(os.path.splitext(p)[0])
                vids.append({"path": p, "size": r.get("size", 0), "modified": r.get("modified", 0),
                             "url": self.base + "/server/files/timelapse/" + urllib.parse.quote(p, safe="/"),
                             "th": (self.base + "/server/files/timelapse/" + urllib.parse.quote(pic, safe="/")) if pic else ""})
            self.post({"type": "timelapse", "vids": vids[:60]})
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "timelapse", "vids": [], "error": repr(exc)[:120]})

    def delete_timelapse(self, paths):
        ok, errs = 0, []
        for p in paths:
            p = str(p or "").strip().lstrip("/")
            if not p or ".." in p.split("/") or "\\" in p or not p.lower().endswith((".mp4", ".webm")):
                errs.append("skipped a bad file name")
                continue
            try:
                http_json(self.base + "/server/files/timelapse/" + urllib.parse.quote(p, safe="/"), method="DELETE", timeout=15)
                ok += 1
                stem = os.path.splitext(p)[0]
                for pic in (stem + ".jpg", stem + "_previewImage.jpg"):
                    try:
                        http_json(self.base + "/server/files/timelapse/" + urllib.parse.quote(pic, safe="/"), method="DELETE", timeout=8)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                errs.append(p + ": " + repr(exc)[:90])
        text = "deleted %d video%s" % (ok, "" if ok == 1 else "s")
        if errs:
            text += "; not deleted: " + "; ".join(errs[:3])
        self.post({"type": "result", "ok": not errs, "text": text})
        self.timelapse()

    def open_timelapse(self, path):
        """Open one video in the person's own browser; only a file in the printer's timelapse folder is allowed."""
        p = str(path or "").strip().lstrip("/")
        if not p or ".." in p.split("/") or not p.lower().endswith((".mp4", ".webm")):
            self.post({"type": "result", "ok": False, "text": "bad video name"})
            return
        webbrowser.open(self.base + "/server/files/timelapse/" + urllib.parse.quote(p, safe="/"))
        self.post({"type": "result", "ok": True, "text": "download started: " + os.path.basename(p)})

    def history(self):
        try:
            jobs = http_json(self.base + "/server/history/list?limit=25&order=desc")["result"]["jobs"]
            try:
                tot = http_json(self.base + "/server/history/totals", timeout=6)["result"]["job_totals"]
            except Exception:  # noqa: BLE001
                tot = {}
            out = []
            for j in jobs:
                md = j.get("metadata") or {}
                cols = [str(c) for c in (md.get("filament_colors") or md.get("extruder_colors") or [])][:HEADS]
                out.append({"id": str(j.get("job_id", "")), "file": j.get("filename", ""), "status": j.get("status", ""),
                            "start": j.get("start_time", 0), "dur": j.get("print_duration", 0),
                            "total": j.get("total_duration", 0), "exists": bool(j.get("exists")),
                            "fu": j.get("filament_used", 0), "g": md.get("filament_weight_total") or 0, "c": cols})
            self.post({"type": "history", "jobs": out, "totals": {
                "n": tot.get("total_jobs", 0), "t": tot.get("total_print_time", 0),
                "f": tot.get("total_filament_used", 0), "l": tot.get("longest_print", 0)}})
            paths = []
            for j in out:
                if j["exists"] and j["file"] not in paths:
                    paths.append(j["file"])
            if paths:
                threading.Thread(target=self.file_meta, daemon=True, args=(paths,)).start()
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "history", "jobs": [], "error": repr(exc)})

    def delete_history(self, ids):
        ok, errs = 0, []
        for i in ids:
            i = str(i or "").strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{1,16}", i):
                errs.append("bad id")
                continue
            try:
                http_json(self.base + "/server/history/job?uid=" + i, method="DELETE", timeout=10)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errs.append(i + ": " + repr(exc)[:80])
        text = "removed %d entr%s from the history" % (ok, "y" if ok == 1 else "ies")
        if errs:
            text += "; not removed: " + "; ".join(errs[:3])
        self.post({"type": "result", "ok": not errs, "text": text})
        self.history()

    def sync_tmt1(self, tool, hexv, mat, midx=None):
        """Write the colour and material into config/tmt1.ini (swatch numbers) so the screen and Fluidd match,
        and push the new value to the tab right away. Without this, the tab's spool icon only caught up on the
        next tmt1.ini poll, which runs every 5th tick (POLL_S * 5, about 10s worst case) - a noticeable lag
        after Save."""
        cidx = nearest_palette(int(hexv, 16))
        if midx is None or not (0 <= midx < len(TOUCH_MATS)) or TOUCH_MATS[midx] != mat:
            midx = next((n for n, z in enumerate(TOUCH_MATS) if z == mat), 0)
        cur = http_text(self.base + "/server/files/config/tmt1.ini")
        new = update_tmt1(cur, tool, cidx, midx)
        if new != cur:
            upload_config(self.base, "", "tmt1.ini", new)      # only this one file, only the [slot] lines of one toolhead
        self.slots = parse_slots(new)
        self.has_tmt1 = True   # a successful tmt1.ini write confirms this printer has a factory screen board
        if self.last:
            self.post({"type": "state", "base": self.base, "status": self.last, "time": time.time(), "cams": self.cams,
                       "slots": merge_saved(self.slots, self.last), "messages": self.messages, "hasTmt1": self.has_tmt1,
                       "lightOn": self.light_state, "helperOk": self.helper_ok})
        return TOUCH_PALETTE[cidx][0]

    def action(self, p):
        """Controls card. The page sends a kind and numbers; the G-code is built here from checked values only."""
        try:
            kind = str(p.get("kind", ""))
            state = self.print_state
            if kind == "temp":
                target = str(p.get("target", ""))
                val = float(p.get("value"))
                if target == "bed":
                    if not 0 <= val <= 120:
                        raise ValueError("bed 0 to 120 C only")
                    script, text = f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={val:.0f}", f"bed {val:.0f} C"
                elif target == "chamber":
                    if not 0 <= val <= 70:
                        raise ValueError("chamber 0 to 70 C only")
                    script, text = f"SET_HEATER_TEMPERATURE HEATER=chamber TARGET={val:.0f}", f"chamber {val:.0f} C"
                else:
                    i = int(target)
                    if i not in range(HEADS) or not 0 <= val <= 300:
                        raise ValueError("toolhead 1 to 4, 0 to 300 C only")
                    script = f"SET_HEATER_TEMPERATURE HEATER={EXTRUDERS[i]} TARGET={val:.0f}"
                    text = f"toolhead {i + 1} {val:.0f} C"
            elif kind == "fan":
                name = str(p.get("name", ""))
                val = float(p.get("value"))
                if name not in ("part", "auxiliary", "air") or not 0 <= val <= 100:
                    raise ValueError("fan part/auxiliary/air, 0 to 100 % only")
                if name == "part":
                    script = f"M106 S{round(val * 2.55)}"
                else:
                    fan = "auxiliary_fan" if name == "auxiliary" else "air_fan"
                    script = f"SET_FAN_SPEED FAN={fan} SPEED={val / 100:.2f}"
                text = f"{name} fan {val:.0f} %"
            elif kind == "speed":
                val = float(p.get("value"))
                if not 10 <= val <= 300:
                    raise ValueError("speed factor 10 to 300 % only")
                script, text = f"M220 S{val:.0f}", f"speed factor {val:.0f} %"
            elif kind == "home":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                script, text = "G28", "home all axes"
            elif kind == "homez":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                script, text = "G28 Z", "home Z"
            elif kind == "motoroff":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                script, text = "M84", "motors off"
            elif kind == "unloadmenu":
                if state == "printing":
                    raise ValueError("not while printing (allowed when paused)")
                script, text = "WM_UNLOAD_MENU", "Unload Menu opened on the printer"
            elif kind == "cooldown":
                script, text = "TURN_OFF_HEATERS", "all heaters off"
            elif kind == "light":
                script, text = "WM_CHAMBER_LIGHT STATE=toggle", "chamber light toggled"
            elif kind == "move":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                axis = str(p.get("axis", "")).lower()
                dist = float(p.get("dist"))
                if axis not in ("x", "y", "z") or dist not in (-50.0, -10.0, -1.0, 1.0, 10.0, 50.0):
                    raise ValueError("axis x/y/z, step 1, 10 or 50 mm only")
                th = (self.last or {}).get("toolhead") or {}
                if axis not in str(th.get("homed_axes", "")).lower():
                    raise ValueError(axis.upper() + " is not homed: press Home all first")
                pos = th.get("position") or []
                lo, hi = th.get("axis_minimum") or [], th.get("axis_maximum") or []
                idx = "xyz".index(axis)
                if len(pos) < 3 or len(lo) < 3 or len(hi) < 3:
                    raise ValueError("position or limits not known yet")
                target = min(max(pos[idx] + dist, lo[idx]), hi[idx])
                if abs(target - pos[idx]) < 0.01:
                    raise ValueError(axis.upper() + " is already at its limit")
                feed = 600 if axis == "z" else 6000
                script = f"G90\nG1 {axis.upper()}{target:.2f} F{feed}"
                text = f"{axis.upper()} to {target:.1f}"
            elif kind == "extrude":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                dist = float(p.get("dist"))
                if dist not in (-10.0, -5.0, 5.0, 10.0):
                    raise ValueError("5 or 10 mm only")
                st = self.last or {}
                name = (st.get("toolhead") or {}).get("extruder", "extruder")
                temp = float((st.get(name) or {}).get("temperature", 0))
                if temp < 175:
                    raise ValueError(f"nozzle is {temp:.0f} C: heat it to 175 C or more first")
                script = f"G92 E0\nG1 E{dist:.0f} F300"
                text = ("extrude " if dist > 0 else "retract ") + f"{abs(dist):.0f} mm"
            elif kind == "spool":
                t = int(p.get("t"))
                mat = int(p.get("mat"))
                midx = p.get("midx")
                midx = None if midx is None else int(midx)
                if midx is not None:
                    # the list entry the person picked: vanilla Klipper (has _ZR_SET_TOOL_INFO) 0-26, factory screen list 0-15
                    top = len(TOUCH_MATS) if "gcode_macro _ZR_SET_TOOL_INFO" in (self.last or {}) else 16
                    if midx == -1:
                        mat = 0
                    elif midx in range(top):
                        mat = TOUCH_MATS[midx]
                    else:
                        raise ValueError("material %d is not in this printer's list" % midx)
                temp = int(p.get("temp"))
                hexv = str(p.get("hex", "")).lstrip("#").upper()
                if t not in range(HEADS) or mat not in range(0, 16) or not (temp == 0 or 150 <= temp <= 300) \
                        or not re.fullmatch(r"[0-9A-F]{6}", hexv):
                    raise ValueError("toolhead 1-4, material 0-15, colour RRGGBB, temperature 0 or 150-300 only")
                script = f"_ZR_SET_TOOL_INFO T={t} HEX={hexv} MAT={mat} TEMP={temp}"
                legacy = f"ZR_SET_TOOL_INFO T={t} HEX={hexv} MAT={mat} TEMP={temp}"
                mname = ZR_MATERIALS[midx] if midx is not None and 0 <= midx < len(ZR_MATERIALS) else SPOOL_MATS.get(mat, 'empty')
                text = f"toolhead {t + 1}: {mname} #{hexv}" + (f" {temp} C" if temp else "")
            elif kind == "dry":
                if p.get("on"):
                    temp = int(p.get("temp"))
                    dur = int(p.get("dur"))
                    if not 35 <= temp <= 65 or not 10 <= dur <= 720:
                        raise ValueError("dryer 35 to 65 C and 10 to 720 minutes only")
                    script, text = f"_ACE_WM_DRY_START TEMP={temp} DURATION={dur}", f"ACE dryer {temp} C for {dur} min"
                else:
                    script, text = "_ACE_WM_DRY_STOP", "ACE dryer stop"
            elif kind == "acepin":
                name, on = str(p.get("name", "")), 1 if p.get("on") else 0
                if name == "pro":
                    if state in ("printing", "paused"):
                        raise ValueError("not while printing")
                    script, text = f"SET_PIN PIN=ACE_Pro VALUE={on}", "ACE Pro " + ("on" if on else "off")
                elif name == "auto":
                    script, text = f"_ACE_WM_AUTO ENABLE={on}", "ACE auto load " + ("on" if on else "off")
                else:
                    raise ValueError("ACE switch pro/auto only")
            elif kind == "endless":
                name, on = str(p.get("name", "")), 1 if p.get("on") else 0
                key = {"enable": "ENABLE", "auto": "AUTO", "tangle": "TANGLE"}.get(name)
                if key is None:
                    raise ValueError("endless switch enable/auto/tangle only")
                script, text = f"_WM_ENDLESS_SET {key}={on}", "endless spool " + name + (" on" if on else " off")
            elif kind == "toolmap":
                if state in ("printing", "paused"):
                    raise ValueError("not while printing")
                mapping = clean_map(p.get("map"))
                if mapping is None:
                    raise ValueError("four toolheads 0-3 only")
                lines = []
                for t, v in enumerate(mapping):
                    lines.append(f"SAVE_VARIABLE VARIABLE=box_modify_t{t} VALUE={v}")
                    lines.append(f"SAVE_VARIABLE VARIABLE=box_modify_t{t}_backup VALUE={v}")
                script = "\n".join(lines)
                text = "tool map " + ", ".join(f"T{t}\u2192T{v}" for t, v in enumerate(mapping))
            elif kind == "endless_go":
                frm, to = int(p.get("frm")), int(p.get("to"))
                if frm not in range(HEADS) or to not in range(HEADS) or frm == to:
                    raise ValueError("two different toolheads 1-4 only")
                if state != "paused":
                    raise ValueError("the print must be paused")
                script = f"_WM_ENDLESS_CONTINUE FROM={frm} TO={to}"
                text = f"endless spool: toolhead {frm + 1} to toolhead {to + 1}"
            else:
                raise ValueError("unknown control")
            # Moonraker answers a gcode POST only when the command has finished, so homing and moves need minutes.
            tmo = 240 if kind in ("home", "homez", "move", "extrude", "endless_go", "unloadmenu") else 20
            note = ""
            light_live_state = None
            if kind == "light" and "gcode_macro WM_CHAMBER_LIGHT" not in (self.last or {}):
                # factory firmware: no Klipper macro for it, try the optional live helper's /light
                # endpoint instead (runs the same wm-chamber-light script the on-screen button uses).
                light_live_state = chamber_light_live(self.base, "toggle")
                if light_live_state is None:
                    raise ValueError("this printer has no WM_CHAMBER_LIGHT macro (factory firmware): use the printer's "
                                      "own light switch, or install the optional wm-screen-sync-http helper (see the "
                                      "README's 'Factory printers: live screen refresh' section) to control it from here")
                script = ""   # handled live above, no gcode macro to run
            if kind == "unloadmenu" and "gcode_macro WM_UNLOAD_MENU" not in (self.last or {}):
                raise ValueError("this printer has no WM_UNLOAD_MENU macro (factory firmware)")
            try:
                if kind == "spool" and "gcode_macro _ZR_SET_TOOL_INFO" not in (self.last or {}):
                    script = ""    # factory printer: no _ZR_SET_TOOL_INFO macro, only the screen's tmt1.ini is updated below
                try:
                    if script:
                        http_json(self.base + "/printer/gcode/script?script=" + urllib.parse.quote(script, safe=""), post=True, timeout=tmo)
                except TimeoutError:
                    raise
                except Exception:
                    if kind != "spool":
                        raise
                    http_json(self.base + "/printer/gcode/script?script=" + urllib.parse.quote(legacy, safe=""), post=True, timeout=tmo)
            except TimeoutError:
                self.post({"type": "result", "ok": True, "text": text + " sent, the printer is still working on it"})
                return
            if kind == "spool" and mat > 0:
                try:
                    near = self.sync_tmt1(t, hexv, mat, midx)
                    note = " (screen and Fluidd show " + near + ")"
                    if "gcode_macro _ZR_SET_TOOL_INFO" not in (self.last or {}):
                        if refresh_screen_live(self.base):
                            note += ". The factory touchscreen is updated now"
                        else:
                            note += (". The factory touchscreen reads it when its app restarts"
                                     " (an optional live-refresh helper is available - see the plugin README)")
                except Exception as exc:
                    note = " - but the screen and Fluidd colour list (tmt1.ini) was NOT updated: " + repr(exc)
            if kind == "light" and light_live_state is not None:
                self.light_state = light_live_state
                self.post({"type": "result", "ok": True, "text": "chamber light is now " + light_live_state,
                           "lightOn": light_live_state})
                return
            self.post({"type": "result", "ok": True, "text": text + " sent" + note})
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "result", "ok": False, "text": "control: " + repr(exc)})

    def command(self, name, filename=""):
        """pause / resume / cancel / start. Checks the current state first so a wrong click does nothing."""
        state = self.print_state
        rules = {"pause": ("printing",), "resume": ("paused",), "cancel": ("printing", "paused"),
                 "start": ("standby", "complete", "cancelled", "error")}
        if name not in rules:
            return
        if state not in rules[name]:
            self.post({"type": "result", "ok": False, "text": f"{name}: not possible while the printer is '{state}'"})
            return
        try:
            if name == "start":
                if not filename or ".." in filename:
                    raise ValueError("no file name")
                url = self.base + "/printer/print/start?filename=" + urllib.parse.quote(filename, safe="")
            else:
                url = self.base + "/printer/print/" + name
            http_json(url, post=True)
            self.post({"type": "result", "ok": True, "text": f"{name} sent" + (f": {filename}" if filename else "")})
        except Exception as exc:  # noqa: BLE001
            self.post({"type": "result", "ok": False, "text": f"{name}: " + repr(exc)})


PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
:root{--g:#00AE42;--panel:rgba(128,128,128,.07);--hd:rgba(128,128,128,.14);--key:#3b3f47;--keyh:#4a4f59;--keyb:#565b66;--keyt:#f2f3f5;--keyin:#2a2d33;--red:#ff7a75}

body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;background:var(--orca-bg);color:var(--orca-fg);font-size:13px;display:flex;min-height:100vh}
#nav{width:178px;flex:none;border-right:1px solid var(--orca-border);padding:12px 0;background:var(--panel)}
#navName{padding:4px 16px 14px;font-weight:600;font-size:14px;display:flex;align-items:center;gap:8px}
#navName .dot{margin:0}
.ni{padding:11px 16px;cursor:pointer;color:var(--orca-muted);border-left:3px solid transparent;font-size:13px}
.ni:hover{background:var(--hd)}
.ni.sel{color:var(--orca-fg);font-weight:600;border-left-color:var(--g);background:var(--hd)}
#content{flex:1;min-width:0;padding:12px 16px 24px}
input,select{padding:6px 10px;background:var(--keyin);color:var(--orca-fg);border:1px solid var(--keyb);border-radius:8px;font-size:12px}
input:focus,select:focus{outline:none;border-color:var(--g)}
input{width:130px}
button{padding:7px 16px;border:1px solid var(--keyb);border-radius:8px;background:var(--key);color:var(--keyt);cursor:pointer;font-size:12px;font-weight:500}
button:hover:not(:disabled){border-color:var(--g);background:var(--keyh)}
button:active:not(:disabled){background:var(--g);border-color:var(--g);color:#fff}
button.pri{background:var(--g);border-color:var(--g);color:#fff}
button.pri:hover:not(:disabled){background:#009a3b}
button:disabled{opacity:.5;cursor:default}
button.danger{border-color:#d9534f;color:#ff7a75}
button.danger:hover:not(:disabled){background:#d9534f;border-color:#d9534f;color:#fff}
.swt{position:relative;display:inline-block;width:40px;height:22px;vertical-align:middle;cursor:pointer}
.swt input{opacity:0;width:0;height:0;position:absolute}
.swt span{position:absolute;inset:0;border-radius:11px;background:var(--keyb);transition:background .15s}
.swt span:after{content:"";position:absolute;left:3px;top:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:left .15s}
.swt input:checked+span{background:var(--g)}
.swt input:checked+span:after{left:21px}
.seg{display:inline-flex;border:1px solid var(--keyb);border-radius:16px;overflow:hidden}
.seg span{padding:6px 15px;font-size:12px;cursor:pointer;color:var(--keyt)}
.seg span.on{background:var(--g);color:#fff}
.mvbox{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.mvcol{display:grid;gap:8px;justify-items:stretch}
.mvcap{font-size:11px;color:var(--orca-muted);text-align:center;margin-bottom:6px}
.k{display:flex;align-items:center;justify-content:center;gap:6px;min-width:52px;height:44px;border-radius:8px;background:var(--key);border:1px solid var(--keyb);color:var(--keyt);cursor:pointer;font-size:13px;font-weight:500;user-select:none}
.k:hover{border-color:var(--g);background:var(--keyh)}.k:active{background:var(--g);border-color:var(--g);color:#fff}
.k.on{background:var(--g);border-color:var(--g);color:#fff}
.dial path.w{fill:var(--key);stroke:var(--orca-bg);stroke-width:3;cursor:pointer}
.dial path.w:hover{fill:var(--g)}
.dial text{fill:var(--keyt);font-size:14px;font-weight:500;pointer-events:none}
.dial circle.c{fill:var(--g);stroke:var(--orca-bg);stroke-width:3;cursor:pointer}
.dial circle.c:hover{fill:#009a3b}
.muted{color:var(--orca-muted);font-size:12px}
.panel{border:1px solid var(--orca-border);border-radius:4px;background:var(--panel);margin-bottom:12px;overflow:hidden}
.panel>.ph{display:flex;align-items:center;gap:8px;padding:7px 14px;background:var(--hd);color:var(--orca-muted);font-size:12px}
.panel>.ph .sp{flex:1}
.panel>.pb{padding:12px 14px}
.live{display:inline-flex;align-items:center;gap:5px;padding:1px 9px;border-radius:10px;background:rgba(0,0,0,.45);color:#fff;font-size:11px}
.live:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--g)}
.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.st{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:12px;align-items:start}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (max-width:980px){.st,.two{grid-template-columns:minmax(0,1fr)}#nav{width:120px}}
.big{font-size:22px;font-weight:600;line-height:1.2}
.lbl{font-size:11px;color:var(--orca-muted)}
.state{display:inline-block;padding:2px 12px;border-radius:12px;font-weight:600;font-size:12px;border:1px solid var(--orca-border)}
.s-printing{background:var(--g);color:#fff;border-color:var(--g)}.s-paused{background:#d9a21b;color:#fff}.s-error{background:#d33;color:#fff}.s-complete{background:#2b7bd8;color:#fff}
.prog{height:6px;border-radius:3px;background:var(--orca-border);overflow:hidden;margin:8px 0}
.prog>div{height:100%;background:var(--g);width:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:middle}
.on{background:var(--g)}.off{background:#888}.red{background:#d33}
.sw{display:inline-block;width:20px;height:20px;border-radius:50%;border:1px solid var(--orca-border);vertical-align:middle;margin-right:6px}
.kv{display:flex;justify-content:space-between;padding:3px 0}
.acegrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.acecard{background:var(--panel);border-radius:10px;padding:12px 14px}.acecap{font-weight:600;margin-bottom:6px}
.acewarn{background:rgba(255,122,117,.16);border:1px solid var(--red);border-radius:10px;padding:10px 14px;margin-bottom:10px;font-weight:600}
.cam{display:block;width:100%;height:auto;min-height:120px;max-height:560px;object-fit:contain;background:#000}
.camwrap{padding:0}
.camfoot{padding:5px 14px;border-top:1px solid var(--orca-border)}
.err{color:#d33}.ok{color:var(--g)}
.spark{display:block;width:100%;height:22px;margin-top:2px}
.badge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;border:1px solid var(--orca-border);margin-right:4px}
.b-ok{background:var(--g);color:#fff;border-color:var(--g)}.b-no{background:#888;color:#fff;border-color:#888}
#toast{display:none;position:fixed;right:16px;bottom:16px;padding:10px 14px;border-radius:6px;background:#2b7bd8;color:#fff;font-weight:600;z-index:9}
#confirm{display:none;border:1px solid #d9a21b;border-radius:4px;padding:10px 14px;margin-bottom:12px;background:rgba(217,162,27,.12)}
#helperCard{display:none;border:1px solid var(--orca-border);border-radius:10px;padding:12px 14px;margin:12px 0;background:var(--panel)}
#helperCard .hdr{font-weight:600;margin-bottom:4px}
#helperCard .sub{font-size:12px;color:var(--orca-muted);margin-bottom:10px;line-height:1.5}
#helperInstallBtn{display:inline-flex;align-items:center;gap:6px}
#helperOverlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20;align-items:center;justify-content:center}
#helperModal{background:var(--orca-bg);border:1px solid var(--orca-border);border-radius:10px;padding:18px 20px;max-width:380px;width:92%}
#helperModal .hwarn{background:rgba(217,162,27,.14);border:1px solid #d9a21b;border-radius:6px;padding:8px 10px;font-size:12px;line-height:1.5;margin:10px 0 14px}
#helperModal label{font-size:11px;color:var(--orca-muted);display:block;margin-bottom:3px}
#helperModal .frow{display:flex;gap:10px;margin-bottom:14px}
#helperModal .frow>div{flex:1}
#helperModal input{width:100%;box-sizing:border-box}
#helperModal .brow{display:flex;gap:8px;justify-content:flex-end}
#helperResult{font-size:12px;margin-top:8px;line-height:1.5;white-space:pre-wrap}
.file{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid var(--orca-border)}
.msg{font-family:Menlo,monospace;font-size:11px;padding:4px 0;border-top:1px solid var(--orca-border);word-break:break-all}
.ctl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}
.ctl .lbl{width:64px}
.ctl input[type=number]{width:70px}
.tgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px}
.tcell .big{font-size:20px}
#heads,#aceSlots{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
#aceSlots .slot{cursor:default}
.slot{cursor:pointer;text-align:center;min-width:0}
.slot .spl{height:84px;border-radius:5px;border:2px solid transparent;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:15px;position:relative}
.slot .spl small{font-weight:500;font-size:11px;opacity:.85}
.slot.empty .spl{border:2px dashed var(--orca-border);color:var(--orca-muted);font-weight:500;font-size:12px}
.slot.act .spl{border-color:var(--g);box-shadow:0 0 0 2px var(--g)}
.slot .pen{position:absolute;right:5px;bottom:3px;font-size:12px;opacity:.8}
.slot .tl{font-weight:600;margin-top:6px;font-size:12px}
.mv{min-width:44px;padding:6px 8px;border-radius:6px}
.slot .tt{font-size:12px;color:var(--orca-muted)}
h3{margin:0 0 6px;font-size:13px}

/* v0.11 skin: modern cards, rail, ring, round spools (palette unchanged) */
:root{--card:rgba(128,128,128,.08);--card2:rgba(128,128,128,.15);--shadow:0 6px 20px rgba(0,0,0,.18)}
body{font-family:Inter,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;font-size:13.5px;-webkit-font-smoothing:antialiased}
#nav{width:190px;padding:18px 0}
#navName{padding:2px 20px 20px;font-size:15px;font-weight:700}
.ni{margin:0 12px 4px;padding:10px 14px;border-radius:10px;border-left:none;font-weight:500}
.ni.sel{background:rgba(0,174,66,.16);box-shadow:inset 3px 0 0 var(--g);font-weight:600}
#content{padding:20px 26px 28px}
.panel{border-radius:16px;background:var(--card);box-shadow:var(--shadow);margin-bottom:16px}
.panel>.ph{background:transparent;padding:16px 20px 0;font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
.panel>.pb{padding:14px 20px 20px}
input,select{border-radius:10px;padding:8px 12px}
button{border-radius:10px;padding:8px 16px;font-weight:600}
.state{padding:5px 14px;border-radius:999px;font-size:12px}
.top{gap:10px;margin-bottom:18px}
.st{gap:16px}.two{gap:16px}
.pr{display:flex;gap:24px;align-items:center}.prc{flex:1;min-width:0}
.ring{width:112px;height:112px;flex:none;border-radius:50%;background:conic-gradient(var(--g) 0 0%,rgba(128,128,128,.25) 0);display:grid;place-items:center}
.ring b{width:86px;height:86px;border-radius:50%;background:var(--orca-bg);display:grid;place-items:center;font-size:22px;font-weight:700}
.prog{display:none}
.tcell{background:var(--card2);border-radius:12px;padding:12px 14px}
.tgrid{gap:12px}
#heads{gap:14px}
.slot{background:var(--card2);border-radius:14px;padding:14px 8px 12px;border:2px solid transparent}
.slot.act{border-color:var(--g);box-shadow:0 0 0 3px rgba(0,174,66,.18)}
.slot .spl{width:84px;height:84px;border-radius:50%;margin:0 auto;border:2px solid rgba(255,255,255,.12)}
.slot.act .spl{box-shadow:none;border-color:rgba(255,255,255,.12)}
.slot.empty .spl{border:2px dashed var(--orca-border)}
.slot .spl small{font-size:10px}
.slot .spl.sp{border:0!important;overflow:visible}.slot .spl.sp small{position:relative;color:#fff}
.slot .tl{margin-top:10px;font-size:13px}
.acecard{background:var(--card2);border-radius:12px}
.kv{gap:14px}.kv span:last-child{white-space:nowrap}
.slot .tt{font-size:11px}
.acewarn{border-radius:12px}
.k{border-radius:10px}
.cam{border-radius:0 0 12px 12px}

.cx .cxc{background:rgba(128,128,128,.10);border-radius:14px;padding:14px 16px;margin-bottom:12px}
.cxl{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--orca-muted);margin-bottom:8px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 12px}.chip{padding:6px 13px;border-radius:999px;background:var(--key);color:var(--keyt);font-size:12px;cursor:pointer}.chip:hover{background:var(--keyh)}.chip.on{background:var(--g);color:#fff;font-weight:700}
.stp{display:flex;align-items:center;gap:10px}.stp .sb{width:42px;height:42px;border-radius:12px;font-size:22px;padding:0;background:var(--key);color:var(--keyt);border:1px solid var(--keyb)}
.stp input{flex:1;min-width:0;height:42px;border-radius:12px;text-align:center;font-size:20px;font-weight:700;background:var(--keyin);color:var(--keyt);border:1px solid var(--keyb)}
.stp .go,.btns button{height:42px;border-radius:12px;font-weight:700;padding:0 22px;border:0;cursor:pointer}.stp .go{background:var(--g);color:#fff}
.pre{display:flex;gap:6px;margin-top:10px}.pre span{padding:4px 11px;border-radius:8px;background:var(--key);color:var(--keyt);font-size:11px;cursor:pointer}.pre span:hover{background:var(--keyh)}
.sl{display:flex;align-items:center;gap:12px;margin:10px 0}.sl .n{width:84px;color:var(--orca-muted)}.sl .p{width:52px;text-align:right;font-weight:700}
.sl input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:3px;background:var(--keyb);outline:0}
.sl input[type=range]::-webkit-slider-thumb,.hue::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.5);cursor:pointer;border:0}
.btns{display:flex;gap:10px}.btns button{flex:1;background:var(--key);color:var(--keyt);border:1px solid var(--keyb)}.btns button:hover{background:var(--keyh)}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:50;align-items:center;justify-content:center}
.mdl{width:380px;max-height:94vh;overflow:auto;background:#2a2d33;color:var(--keyt);border-radius:18px;padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.6);border:1px solid var(--keyb)}
.mdl .mh{display:flex;align-items:center;gap:12px;font-size:16px;margin-bottom:6px}.mdl .lbl{font-size:11px;color:var(--orca-muted);margin:12px 0 4px;text-transform:uppercase;letter-spacing:.06em}
.mdl select,.mdl input[type=number],.mdl input[type=text]{width:100%;height:36px;border-radius:10px;background:var(--keyin);color:var(--keyt);border:1px solid var(--keyb);padding:0 12px;font-size:13px}
.prev{width:40px;height:40px;border-radius:50%;border:3px solid var(--keyb);display:inline-block;background:#fff}
.swr{display:flex;gap:8px;flex-wrap:wrap}.rsw{width:28px;height:28px;border-radius:50%;border:2px solid rgba(255,255,255,.2);cursor:pointer;display:block}.rsw:hover{border-color:#fff}
.svb{height:150px;border-radius:12px;position:relative;margin-top:14px;cursor:crosshair;user-select:none}.svb b{position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;border:2px solid #fff;border-radius:50%;box-shadow:0 0 4px #000;pointer-events:none}
.hue{-webkit-appearance:none;appearance:none;width:100%;height:12px;border-radius:6px;margin:14px 0 6px;background:linear-gradient(to right,red,#ff0,#0f0,#0ff,#00f,#f0f,red);outline:0}
.hx{display:flex;align-items:center;gap:6px;margin-top:6px}.hx .hs{font-weight:700}.hx input{text-transform:uppercase;font-weight:700}
.fb{display:flex;gap:10px;margin-top:16px}.fb button{flex:1;height:42px;border-radius:12px;font-weight:700;border:0;cursor:pointer;background:var(--key);color:var(--keyt)}.fb .pri{background:var(--g);color:#fff}

.thd,.trow{display:grid;grid-template-columns:78px 58px minmax(0,1fr) 52px;gap:8px;align-items:center}
.thd{font-size:10px;color:var(--orca-muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.trow{margin:7px 0}.trow .tn{font-weight:600}.trow .tc{color:var(--orca-muted)}
.trow .ti{display:flex;align-items:center;height:36px;border-radius:10px;background:var(--keyin);border:1px solid var(--keyb);padding:0 10px}
.trow .ti input{flex:1;min-width:0;border:0;background:transparent;color:var(--keyt);font-size:15px;font-weight:700;outline:0;height:34px}
.trow .ti input::-webkit-inner-spin-button{opacity:.4}.trow .ti em{color:var(--orca-muted);font-style:normal;font-size:11px}
.trow .go{height:36px;border-radius:10px;background:var(--g);color:#fff;font-weight:700;border:0;cursor:pointer;padding:0}
.pm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px;padding-top:14px;border-top:1px solid rgba(128,128,128,.25)}
.pm>div{background:rgba(128,128,128,.12);border-radius:10px;padding:8px 10px}.pm small{display:block;color:var(--orca-muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px}.pm span{font-weight:700}

.cxrow{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:10px;align-items:start}
.cxrow .cxc{padding:12px 10px;margin-bottom:10px}.cxcol .btns{margin-top:2px}.cxcol .btns button{padding:0 6px;font-size:12px}
.thd,.trow{grid-template-columns:minmax(0,56px) minmax(0,34px) 56px 38px!important;gap:5px!important;justify-content:start;font-size:12px}
.thd{font-size:9px}.trow .ti{width:56px;padding:0 2px}.trow .ti input{text-align:center;font-size:14px}
.trow .go{font-size:12px;width:38px;height:34px}.trow .tc{font-size:11px}.trow .tn{font-size:12px}
.trow .ti input::-webkit-inner-spin-button,.trow .ti input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}.trow .ti em{display:none}
.cxcol .sl{flex-wrap:wrap;gap:2px 8px;margin:8px 0}.cxcol .sl .n{flex:1;width:auto}.cxcol .sl .p{width:auto}.cxcol .sl input[type=range]{order:3;flex:0 0 100%}
.cxcol .pre{flex-wrap:wrap}
@media (max-width:980px){.cxrow{grid-template-columns:minmax(0,1fr)}}
.swrow{display:flex;align-items:center;gap:12px;padding:9px 0;border-top:1px solid var(--orca-border)}.swrow>div:first-child{flex:1}.swrow b{display:block;font-weight:600}.swrow small{display:block;color:var(--orca-muted);font-size:11px;margin-top:2px}
.tg{position:relative;width:40px;height:22px;flex:none}.tg input{opacity:0;width:100%;height:100%;margin:0;position:absolute;inset:0;cursor:pointer;z-index:1}.tg i{position:absolute;inset:0;border-radius:11px;background:rgba(128,128,128,.45);transition:.15s}.tg i:after{content:"";position:absolute;left:3px;top:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.15s}.tg input:checked+i{background:var(--g)}.tg input:checked+i:after{left:21px}.tg input:disabled{cursor:default}.tg input:disabled+i{opacity:.45}
.endrow{display:flex;align-items:center;gap:18px;margin-top:12px}.chips{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.chip{display:inline-flex;align-items:center;gap:8px;padding:5px 12px;border-radius:16px;background:var(--hd);font-size:12px}.chip .cd{width:11px;height:11px;border-radius:50%;border:1px solid rgba(128,128,128,.6)}.chip .mt{color:var(--orca-muted)}.chip .ok{color:#3fbf63}.chip .wn{color:#e0a63a}.chip.fd{opacity:.6}
.endoffer{display:flex;align-items:center;gap:12px;margin-top:12px;padding:9px 12px;border-radius:8px;background:rgba(224,166,58,.18);font-size:12px}.endoffer button{margin-left:auto}
.fil2{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;align-items:start}.fil2>.panel{margin-bottom:16px}
.mtop{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.seg{display:inline-flex;border:1px solid var(--orca-border);border-radius:8px;overflow:hidden}.seg button{border:0;border-radius:0;padding:4px 12px;background:transparent}.seg button.on{background:var(--hd)}
.mpos{position:relative}.msaved{position:absolute;z-index:5;top:34px;left:0;min-width:220px;background:var(--key);color:var(--keyt);border:1px solid var(--keyb);border-radius:10px;padding:8px;box-shadow:var(--shadow)}.msaved .it{display:flex;gap:6px;align-items:center;padding:3px 0}.msaved .it span{flex:1;cursor:pointer}.msaved input{width:110px}
button.lnk{border:0;background:transparent;color:#4aa3ff;font-weight:600;letter-spacing:.04em;text-transform:uppercase;font-size:11px;padding:4px 6px}
.mrow{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:8px 8px;margin-bottom:8px;border-radius:8px;background:var(--hd);border-left:3px solid #EF9F27}.mrow.ok{border-left-color:#3fbf63}
.mdot{width:20px;height:20px;border-radius:50%;border:1px solid rgba(128,128,128,.6);flex:none}.mname{flex:1;min-width:0;line-height:1.3}.mname small{display:block;color:var(--orca-muted);letter-spacing:.08em}
.mheads{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;flex:1 1 100%;order:3;min-width:0}.mh{display:inline-flex;align-items:center;gap:8px;min-height:42px;padding:4px 6px;flex:1;min-width:0;justify-content:center;border-radius:6px;border:1px solid var(--orca-border);background:transparent;line-height:1.1;text-align:left}.mh .hd{width:16px;height:16px;border-radius:50%;border:1px solid rgba(128,128,128,.6);flex:none}.mh b{display:block;font-size:15px;font-weight:600}.mh small{display:none;font-size:11px}.mh.sel{border:2px solid #4aa3ff;background:rgba(74,163,255,.18)}.mh.off{opacity:.45;cursor:default}
.mst{font-weight:700;letter-spacing:.06em;font-size:10px;color:#e0a63a;text-align:right;flex:none;order:2}.mrow.ok .mst{color:#3fbf63}
.mw{display:flex;gap:10px;align-items:center;margin-bottom:8px;padding:10px 12px;border-radius:8px;background:rgba(224,166,58,.16);color:#e0a63a;font-size:12px}
.mfoot{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.mchk{font-size:11px;color:var(--orca-muted);flex:1 1 100%;min-width:0;display:flex;gap:8px;align-items:flex-start;line-height:1.35}.mchk input{flex:none;width:16px;height:16px;margin:1px 0 0}
.mmap{width:100%;height:190px}.mcol{display:flex;justify-content:space-between;max-width:640px;margin:0 auto;font-size:11px;letter-spacing:.08em;color:var(--orca-muted)}
.fil2 .endrow{flex-direction:column;align-items:flex-start;gap:10px}.fil2 .chips{width:100%}.fil2 .chip{flex-wrap:wrap;gap:4px 6px}.mname b{white-space:nowrap}
.fil2{margin-bottom:24px}.mname b{font-size:15px}.mst{font-size:11px}@media(min-width:620px){.mheads{grid-template-columns:repeat(4,minmax(0,1fr))}.mh small{display:block}}@media(min-width:900px){.mrow{flex-wrap:nowrap}.mname{flex:none;width:130px}.mheads{order:0;flex:1 1 auto}.mst{width:70px}}
/* v0.26 layout A: tighter cards, more in one view; Files page with part pictures */
#content{padding:14px 18px 20px}
.panel{margin-bottom:10px}
.panel>.ph{padding:12px 16px 0}
.panel>.pb{padding:10px 16px 14px}
.st,.two{gap:10px}
.tgrid{gap:8px}
.tcell{padding:8px 12px}
#heads{gap:10px}
.slot{padding:10px 8px 8px}
.slot .spl{width:68px;height:68px}
.slot .tl{margin-top:6px}
.ring{width:92px;height:92px}.ring b{width:70px;height:70px;font-size:19px}
.pr{gap:18px}
.cam{max-height:480px;height:480px;width:auto;max-width:100%;margin:0 auto;background:transparent}
.mvbox{gap:18px}
.frow{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:12px;background:var(--card2);margin-bottom:8px}
.fth{width:64px;height:64px;flex:none;border-radius:10px;background:rgba(128,128,128,.18);object-fit:contain}
.fmid{flex:1;min-width:0}
.fname{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fdots{display:flex;gap:5px;margin-top:5px}
.fdot{width:13px;height:13px;border-radius:50%;border:1px solid rgba(255,255,255,.25)}
.frow button{min-width:74px}
.fchk{width:22px;height:22px;flex:none;cursor:pointer;accent-color:#c0392b}
.fil2{grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:10px}.fil2>.panel{margin-bottom:0}
/* v0.26 themes: Style button cycles Current / A bento graphite / B soft slate */
body[data-th="a"]{--orca-bg:#0e1013;--orca-fg:#e8eaed;--orca-muted:#8b93a1;--orca-border:#262a33;--g:#2dd4a7;--panel:#14171c;--hd:#1b1f26;--card:#171a20;--card2:#1d2128;--shadow:none;--key:#1f232b;--keyh:#2a2f39;--keyb:#323744;--keyt:#e8eaed;--keyin:#12151a}
body[data-th="a"] .panel{border:1px solid #262a33;border-radius:14px}
body[data-th="a"] #nav{background:#0b0d10;border-right:1px solid #1c2027}
body[data-th="a"] .ni.sel{background:rgba(45,212,167,.12);box-shadow:inset 3px 0 0 var(--g)}
body[data-th="a"] .tcell,body[data-th="a"] .slot,body[data-th="a"] .acecard{border:1px solid #262a33}
body[data-th="a"] button.pri,body[data-th="a"] .s-printing,body[data-th="a"] .seg span.on,body[data-th="a"] .chip.on,body[data-th="a"] .stp .go,body[data-th="a"] .b-ok{color:#06201a}
body[data-th="b"]{--orca-bg:#1c1f26;--orca-fg:#eceef2;--orca-muted:#98a0b0;--orca-border:#2c313c;--g:#ff9a3d;--panel:#222630;--hd:#252932;--card:#252932;--card2:#2d323d;--shadow:none;--key:#303541;--keyh:#3b4150;--keyb:#303541;--keyt:#eceef2;--keyin:#20242c}
body[data-th="b"] .panel{border:0;border-radius:20px}
body[data-th="b"] #nav{background:#181b21;border-right:0}
body[data-th="b"] .ni{border-radius:999px}
body[data-th="b"] .ni.sel{background:rgba(255,154,61,.16)}
body[data-th="b"] button,body[data-th="b"] input,body[data-th="b"] select{border-radius:999px}
body[data-th="b"] .tcell,body[data-th="b"] .slot,body[data-th="b"] .acecard{border-radius:18px;border-color:transparent}
body[data-th="b"] button.pri,body[data-th="b"] .s-printing,body[data-th="b"] .seg span.on,body[data-th="b"] .chip.on,body[data-th="b"] .stp .go,body[data-th="b"] .b-ok{color:#2b1700}
body[data-th="b"] .ni.sel,body[data-th="a"] .ni.sel{color:var(--orca-fg)}
</style></head><body>
<div id="pmodal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:60;align-items:center;justify-content:center"><div id="pbox" style="background:var(--card2,#1d2128);border:1px solid var(--keyb);border-radius:14px;padding:18px 20px;width:500px;max-width:92vw;max-height:90vh;overflow:auto"></div></div>
<div id="nav"><div id="navName"><span class="dot off" id="navDot"></span><span id="navTxt">WM Device</span></div>
<div class="ni sel" id="n-status" onclick="showTab('status')">Status</div>
<div class="ni" id="n-files" onclick="showTab('files')">Files</div>
<div class="ni" id="n-hist" onclick="showTab('hist')">History</div>
<div class="ni" id="n-tl" onclick="showTab('tl')">Timelapse</div>
<div class="ni" id="n-msgs" onclick="showTab('msgs')">Messages</div>
<div class="muted" style="padding:16px" id="verbox">v0.29.17</div></div>
<div id="content">
<div class="top">
<select id="preset" onchange="pick()"><option value="">Other address...</option></select>
<input id="addr" placeholder="printer address, e.g. 192.168.1.50"><button class="pri" onclick="connect()">Connect</button><button id="forget" onclick="forget()">Forget</button><button id="thBtn" onclick="cycleTheme()">Style: Current</button>
<span id="conn" class="muted"></span><span id="state" class="state">-</span><span style="flex:1"></span>
<button onclick="refreshCam()">Refresh camera</button> <button id="camBtn" onclick="toggleCam()">Hide camera</button></div>
<div id="err" class="err"></div>
<div id="klip" class="panel" style="display:none;border-color:#d33;padding:10px 14px"></div>
<div id="toast"></div>
<div id="confirm"><span id="confirmText"></span> <button id="confirmYes" class="danger">Yes</button> <button onclick="hideConfirm()">No</button></div>
<div id="spool" class="ov" style="display:none"><div class="mdl">
<div class="mh"><span id="spW" class="prev"></span><b id="spT"></b></div>
<div class="lbl">Material</div><select id="spM"></select>
<div class="lbl" id="spNL">Nozzle temp (blank = none)</div><input id="spN" type="number" min="150" max="300" placeholder="&deg;C">
<div class="lbl">Colour</div><div id="spC" class="swr"></div>
<div id="svBox" class="svb"><b id="svK"></b></div>
<input type="range" id="hue" class="hue" min="0" max="360" step="1" value="0" oninput="hueIn(this.value)">
<div class="hx"><span class="hs">#</span><input id="spH" type="text" maxlength="7" placeholder="RRGGBB" oninput="hexIn(this.value)"></div>
<div id="spNear" class="muted" style="margin-top:6px"></div>
<div class="fb"><button onclick="$('spool').style.display='none'">Cancel</button><button class="pri" onclick="saveSpool()">Save to printer</button></div>
<div class="muted" style="margin-top:8px">Saves with the Pi's _ZR_SET_TOOL_INFO macro (unload, endless spool and the screen's Toolheads card).</div></div></div>

<div id="v-status"><div class="st">
<div>
<div class="panel" id="camBox"><div class="ph"><span>Camera</span><span class="sp"></span><span class="live">Live</span></div>
<img id="cam" class="cam" alt="camera"><div class="camfoot"><span id="camMsg" class="muted"></span></div></div>
<div class="panel"><div class="ph">Printing Progress</div><div class="pb pr"><div id="ring" class="ring"><b id="ringTxt">0%</b></div><div class="prc">
<div class="kv"><b id="file"></b><span id="times" class="muted"></span></div>
<div class="prog"><div id="bar"></div></div>
<div class="kv"><span><span id="pct" class="big ok">-</span> <span id="layer" class="muted"></span></span><span id="res" class="muted"></span></div>
<div style="display:flex;gap:8px;margin-top:8px"><button id="bPause" onclick="ask('pause')">Pause</button><button id="bResume" onclick="ask('resume')">Resume</button><button id="bCancel" class="danger" onclick="ask('cancel')">Stop</button></div></div></div></div>
<div class="panel"><div class="ph">Filament (click a spool to edit)</div><div class="pb"><div id="heads"></div>
<div id="aceSw" style="display:none">
<div class="swrow" id="rowPro"><div><b>ACE Pro power</b><small id="proHint">Off: the printer ignores tool changes and slot moves</small></div><label class="tg"><input type="checkbox" id="swPro" onchange="aceSw('pro',this.checked)"><i></i></label></div>
<div class="swrow" id="rowAuto"><div><b>Auto load</b><small>Start loading when a spool is put in a slot</small></div><label class="tg"><input type="checkbox" id="swAuto" onchange="aceSw('auto',this.checked)"><i></i></label></div>
</div></div></div>
<div class="fil2" id="fil2" style="display:none"><div class="panel" id="endBox" style="display:none"><div class="ph">Endless spool</div><div class="pb">
<div class="swrow" style="border-top:0"><div><b>Endless spool</b><small>Another loaded toolhead takes over only if it has the same colour and material</small></div><label class="tg"><input type="checkbox" id="swEnd" onchange="endSw('enable',this.checked)"><i></i></label></div>
<div class="swrow"><div><b>Swap automatically</b><small>Swap and continue without asking</small></div><label class="tg"><input type="checkbox" id="swEAuto" onchange="endSw('auto',this.checked)"><i></i></label></div>
<div class="swrow"><div><b>Swap on a tangle</b><small>Also swap when a tangle sensor pauses the print</small></div><label class="tg"><input type="checkbox" id="swETang" onchange="endSw('tangle',this.checked)"><i></i></label></div>
<div class="endrow"><svg id="endRing" viewBox="0 0 104 104" width="104" height="104"></svg><div id="endChips" class="chips"></div></div>
<div id="endOffer" class="endoffer" style="display:none"><span id="endOfferTxt"></span><button class="pri" id="endGo" onclick="endGo()">Swap and continue</button></div>
</div></div>
<div class="panel" id="mapBox" style="display:none"><div class="ph">Filament in the file &rarr; toolhead</div><div class="pb">
<div class="muted" id="mSrc" style="margin:0 0 6px"></div>
<div id="mRows"></div><div id="mWarn" style="margin-top:10px"></div>
</div></div></div>
</div>
<div>
<div class="panel"><div class="ph">Control</div><div class="pb">
<div class="tgrid">
<div class="tcell"><div class="lbl">State</div><div class="big" id="st2">-</div></div>
<div class="tcell"><div class="lbl">Active nozzle</div><div class="big" id="nz">-</div><div class="muted" id="nzt"></div></div>
<div class="tcell"><div class="lbl">Bed</div><div class="big" id="bed">-</div><div class="muted" id="bedt"></div><svg class="spark" id="bedsp"></svg></div>
<div class="tcell"><div class="lbl">Chamber</div><div class="big" id="cham">-</div><div class="muted" id="chamt"></div><svg class="spark" id="chamsp"></svg></div></div>
<div style="border-top:1px solid var(--orca-border);margin:10px 0 6px"></div>
<div class="kv"><span>Part fan</span><span id="f0">-</span></div><div class="kv"><span>Auxiliary fan</span><span id="f1">-</span></div><div class="kv"><span>Air filter</span><span id="f2">-</span></div><div class="kv"><span>Speed factor</span><span id="spd">-</span></div></div></div>
<div class="panel"><div class="ph"><span>Move</span></div><div class="pb" id="moveBody"><div class="mvbox">
<svg class="dial" width="224" height="224" viewBox="0 0 170 170" role="img"><title>X and Y jog dial</title>
<circle cx="85" cy="85" r="83" fill="none" stroke="var(--keyb)" stroke-width="1.5"/>
<path class="w" d="M85 85 L28 28 A80 80 0 0 1 142 28 Z" onclick="mv('y',1)"/>
<path class="w" d="M85 85 L142 28 A80 80 0 0 1 142 142 Z" onclick="mv('x',1)"/>
<path class="w" d="M85 85 L142 142 A80 80 0 0 1 28 142 Z" onclick="mv('y',-1)"/>
<path class="w" d="M85 85 L28 142 A80 80 0 0 1 28 28 Z" onclick="mv('x',-1)"/>
<circle class="c" cx="85" cy="85" r="26" onclick="doAct({kind:'home'},'Home all axes? The toolhead will move.')"/>
<text x="85" y="33" text-anchor="middle">Y+</text><text x="143" y="90" text-anchor="middle">X+</text><text x="85" y="152" text-anchor="middle">Y-</text><text x="27" y="90" text-anchor="middle">X-</text><text x="85" y="90" text-anchor="middle" style="fill:#fff;font-size:12px">Home</text></svg>
<div style="position:relative"><div class="mvcap" style="position:absolute;top:-20px;left:0;right:0;margin:0">Z axis</div><div class="mvcol" style="height:224px;grid-template-rows:1fr 1fr"><div class="k" onclick="mv('z',1)" style="height:auto;min-width:64px">&#9650; Z+</div><div class="k" onclick="mv('z',-1)" style="height:auto;min-width:64px">&#9660; Z-</div></div></div>

<div><div class="mvcap">Step (mm)</div><div class="seg" id="mvStep"><span onclick="setStep(1)">1</span><span class="on" onclick="setStep(10)">10</span><span onclick="setStep(50)">50</span></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px"><div class="k" id="btnLight" onclick="act({kind:'light'})">Light</div><div class="k" onclick="doAct({kind:'homez'},'Home Z? The toolhead will move.')">Home Z</div><div class="k" onclick="doAct({kind:'motoroff'},'Motors off?')">Motors off</div><div class="k" id="btnUnload" onclick="act({kind:'unloadmenu'})" style="background:var(--g);border-color:var(--g);color:#fff">Unload Menu</div></div></div>
</div><div class="muted" style="margin-top:10px">Moves apply at once. Not while printing. Z+ and Z- are the printer's Z coordinate.</div><div class="pm"><div><small>X / Y / Z</small><span id="pos">-</span></div><div><small>Homed</small><span id="homed">-</span></div><div><small>Idle timeout</small><span id="idle">-</span></div><div><small>Extrusion factor</small><span id="ext">-</span></div></div></div>
<div id="helperCard"><div class="hdr">Screen live-refresh helper</div><div class="sub" id="helperSub">Not installed. Without it, the Light button can't control the chamber light, and a spool colour/material change saved here only shows up on the touchscreen after its app restarts.</div><button id="helperInstallBtn" class="k" style="background:var(--g);border-color:var(--g);color:#fff;width:auto" onclick="openInstallHelper()">Install helper on printer</button><div id="helperResult"></div></div>
</div>
<div id="helperOverlay"><div id="helperModal">
<div style="font-weight:600;margin-bottom:6px">Install the screen helper on your printer?</div>
<div style="font-size:12px;color:var(--orca-muted);line-height:1.6">This connects to your printer over SSH and installs a small background service (wm-screen-sync-http). It runs independently of Klipper - no restart needed.</div>
<div class="hwarn">Only do this on a printer you own. These are pre-filled with the factory default login - change them if you've set your own.</div>
<div class="frow"><div><label>Username</label><input id="helperUser" type="text" value="t13dp"></div><div><label>Password</label><input id="helperPass" type="password" value="TM88888"></div></div>
<div id="helperModalResult"></div><div class="brow"><button onclick="closeInstallHelper()">Cancel</button><button id="helperGo" style="background:var(--g);border-color:var(--g);color:#fff" onclick="confirmInstallHelper()">Install</button></div>
</div></div>
<div class="panel"><div class="ph">Controls</div><div class="pb cx cxrow">
<div class="cxc"><div class="cxl">Temperature</div>
<div class="thd"><span>Heater</span><span>Now</span><span>Target</span><span></span></div>
<div class="trow"><span class="tn">Tool 1</span><span class="tc" id="tc0">-</span><div class="ti"><input id="ti0" type="number" min="0" max="300" placeholder="0" oninput="TD['0']=1" onkeydown="if(event.key==='Enter')setT('0')"><em>&deg;C</em></div><button class="go" onclick="setT('0')">Set</button></div>
<div class="trow"><span class="tn">Tool 2</span><span class="tc" id="tc1">-</span><div class="ti"><input id="ti1" type="number" min="0" max="300" placeholder="0" oninput="TD['1']=1" onkeydown="if(event.key==='Enter')setT('1')"><em>&deg;C</em></div><button class="go" onclick="setT('1')">Set</button></div>
<div class="trow"><span class="tn">Tool 3</span><span class="tc" id="tc2">-</span><div class="ti"><input id="ti2" type="number" min="0" max="300" placeholder="0" oninput="TD['2']=1" onkeydown="if(event.key==='Enter')setT('2')"><em>&deg;C</em></div><button class="go" onclick="setT('2')">Set</button></div>
<div class="trow"><span class="tn">Tool 4</span><span class="tc" id="tc3">-</span><div class="ti"><input id="ti3" type="number" min="0" max="300" placeholder="0" oninput="TD['3']=1" onkeydown="if(event.key==='Enter')setT('3')"><em>&deg;C</em></div><button class="go" onclick="setT('3')">Set</button></div>
<div class="trow"><span class="tn">Bed</span><span class="tc" id="tcbed">-</span><div class="ti"><input id="tibed" type="number" min="0" max="120" placeholder="0" oninput="TD['bed']=1" onkeydown="if(event.key==='Enter')setT('bed')"><em>&deg;C</em></div><button class="go" onclick="setT('bed')">Set</button></div>
<div class="trow"><span class="tn">Chamber</span><span class="tc" id="tcchamber">-</span><div class="ti"><input id="tichamber" type="number" min="0" max="70" placeholder="0" oninput="TD['chamber']=1" onkeydown="if(event.key==='Enter')setT('chamber')"><em>&deg;C</em></div><button class="go" onclick="setT('chamber')">Set</button></div>
</div>
<div class="cxcol">
<div class="cxc"><div class="cxl">Fans</div>
<div class="sl"><span class="n">Part fan</span><input type="range" id="fp" min="0" max="100" step="5" value="0" oninput="slv(this)" onchange="doFan('part',this.value)"><span class="p" id="fpv">0 %</span></div>
<div class="sl"><span class="n">Auxiliary</span><input type="range" id="fa" min="0" max="100" step="5" value="0" oninput="slv(this)" onchange="doFan('auxiliary',this.value)"><span class="p" id="fav">0 %</span></div>
<div class="sl"><span class="n">Air filter</span><input type="range" id="fr" min="0" max="100" step="5" value="0" oninput="slv(this)" onchange="doFan('air',this.value)"><span class="p" id="frv">0 %</span></div></div>
<div class="cxc"><div class="cxl">Speed</div>
<div class="sl"><span class="n">Factor</span><input type="range" id="sp" min="50" max="200" step="5" value="100" oninput="slv(this)" onchange="doSpeed(this.value)"><span class="p" id="spv">100 %</span></div>
<div class="pre"><span onclick="doSpeed(50)">50</span><span onclick="doSpeed(100)">100</span><span onclick="doSpeed(150)">150</span><span onclick="doSpeed(200)">200</span></div></div>
<div class="btns"><button onclick="doAct({kind:'cooldown'})">Cool down</button></div></div>
</div></div>
<div class="panel" id="aceBox" style="display:none"><div class="ph"><span class="dot off" id="aceDot"></span><span id="aceTitle">ACE Pro</span><span class="sp"></span><span class="muted" id="aceEs"></span></div><div class="pb">
<div id="aceWarn" class="acewarn" style="display:none"></div>
<div class="acegrid"><div class="acecard"><div class="acecap">Dryer</div><div class="kv"><span>Status</span><span id="dryS">-</span></div><div class="kv"><span>Temperature</span><span id="dryT">-</span></div><div class="kv"><span>Time left</span><span id="dryR">-</span></div>
<div class="ctl"><span class="lbl">Temp</span><input id="dryTemp" type="number" min="35" max="65" value="50" style="width:70px"><span class="lbl">Minutes</span><input id="dryDur" type="number" min="10" max="720" value="240" style="width:80px"></div>
<div class="ctl"><button class="pri" onclick="dryGo(true)">Start</button><button onclick="dryGo(false)">Stop</button></div></div>
<div class="acecard"><div class="acecap">Tool map and stats</div><div class="kv"><span>Tool map</span><span id="aceMap">-</span></div><div class="kv"><span>Tool changes</span><span id="aceTc">-</span></div><div class="kv"><span>Purged</span><span id="aceFl">-</span></div><div class="kv"><span>Cuts</span><span id="aceCt">-</span></div><div class="muted">Map editing is not in this version.</div></div></div>
</div></div>
</div></div>
</div>

<div id="v-files" style="display:none"><div class="panel"><div class="ph">Files <span class="sp"></span><button onclick="loadFiles()">Refresh list</button> <button id="delBtn" onclick="askDelete()" style="background:#c0392b;border-color:#c0392b;color:#fff">Delete</button></div><div class="pb"><div id="delbar" style="display:none;margin-bottom:10px;padding:10px 12px;border-radius:10px;border:1px solid #c0392b;background:rgba(192,57,43,.12)"></div><div id="files" class="muted">press Refresh list</div></div></div></div>
<div id="v-hist" style="display:none"><div class="panel"><div class="ph">Print history <span class="sp"></span><button onclick="loadHist()">Refresh</button> <button onclick="askDelHist()" style="background:#c0392b;border-color:#c0392b;color:#fff" id="hDelBtn">Delete</button></div><div class="pb"><div id="htot" style="display:flex;gap:10px;margin-bottom:12px"></div><div id="hdelbar" style="display:none;margin-bottom:10px;padding:10px 12px;border-radius:10px;border:1px solid #c0392b;background:rgba(192,57,43,.12)"></div><div id="hist" class="muted">loading...</div></div></div></div>
<div id="v-tl" style="display:none"><div class="panel"><div class="ph">Timelapse videos <span class="sp"></span><button onclick="loadTL()">Refresh</button> <button onclick="askDelTL()" style="background:#c0392b;border-color:#c0392b;color:#fff" id="tDelBtn">Delete</button></div><div class="pb"><div id="tlplay" style="display:none;margin-bottom:12px"><video id="tlvid" controls style="display:block;margin:0 auto;width:auto;max-width:100%;height:auto;max-height:420px;background:transparent;border-radius:10px"></video><div style="display:flex;justify-content:space-between;margin-top:6px"><span id="tlname" class="muted"></span><button onclick="closeTL()">Close</button></div></div><div id="tdelbar" style="display:none;margin-bottom:10px;padding:10px 12px;border-radius:10px;border:1px solid #c0392b;background:rgba(192,57,43,.12)"></div><div id="tlcount" class="muted" style="margin-bottom:8px"></div><div id="tllist" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px" class="muted">loading...</div></div></div></div>
<div id="v-msgs" style="display:none"><div class="panel"><div class="ph">Messages and errors</div><div class="pb"><div id="msgs" class="muted">-</div></div></div><div class="panel"><div class="ph">Orca API check <span class="sp"></span><button onclick="orca.postMessage({type:'probe'});$('probetxt').value='checking...'">Check Orca API</button></div><div class="pb"><div class="muted" style="margin-bottom:6px">Read only: lists the names Orca offers to plugins. Nothing is changed. Select all and copy the text to send it.</div><textarea id="probetxt" readonly style="width:100%;height:260px;font-family:monospace;font-size:12px"></textarea></div></div></div>
<div class="muted" id="upd" style="margin-top:10px"></div>
</div>
<script>
var BASE="",CAMS=[],camOn=true,STATE="",PENDING=null,HASTMT1=false,LIGHTON=null,HELPEROK=false;
function $(i){return document.getElementById(i)}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function t(v){return (v===undefined||v===null)?"-":(Math.round(v*10)/10).toFixed(1)+" °C"}
function tt(s){s=Math.round(s||0);var h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+"h "+(m<10?"0":"")+m+"m"}
function pc(v){return (v===undefined||v===null)?"-":Math.round(v*100)+" %"}
var FGT=0;
function forget(){var a=$("addr").value.trim(),b=$("forget");if(!a)return;
  if(!FGT){b.textContent="Erase "+a+"? Press again";FGT=setTimeout(function(){FGT=0;b.textContent="Forget"},5000);return}
  clearTimeout(FGT);FGT=0;b.textContent="Forget";
  var s=$("preset");for(var i=s.options.length-1;i>=0;i--){if(s.options[i].value===a)s.remove(i)}
  try{var L=JSON.parse(localStorage.getItem("wmdev_addrs")||"[]").filter(function(x){return x!==a});localStorage.setItem("wmdev_addrs",JSON.stringify(L));if(localStorage.getItem("wmdev_addr")===a)localStorage.removeItem("wmdev_addr")}catch(e){}
  try{orca.postMessage({type:"forget",addr:a})}catch(e){}
  $("addr").value="";s.selectedIndex=s.options.length-1;$("err").textContent="Erased "+a+" from the saved printers.";$("conn").textContent=""}
function pick(){var v=$("preset").value;if(v)$("addr").value=v;connect()}
function syncPreset(a){var s=$("preset"),f=false;for(var i=0;i<s.options.length;i++){if(s.options[i].value===a){s.selectedIndex=i;f=true}}if(!f)s.selectedIndex=s.options.length-1}
var FIRST=true;
var TH={},LASTST="",TOASTT=null;
function pushT(k,v){if(v===undefined||v===null)return;var a=TH[k]||(TH[k]=[]);a.push(v);if(a.length>150)a.shift()}
function spark(a,col){if(!a||a.length<2)return "";var w=200,h=26,mn=Math.min.apply(null,a),mx=Math.max.apply(null,a);if(mx-mn<2){mx=mn+2}
  var pts=a.map(function(v,i){return (i*w/(a.length-1)).toFixed(1)+","+(h-2-(v-mn)/(mx-mn)*(h-4)).toFixed(1)}).join(" ");
  return '<polyline fill="none" stroke="'+col+'" stroke-width="1.5" points="'+pts+'"/>'}
function setSpark(id,k,col){var e=$(id);if(!e)return;e.setAttribute("viewBox","0 0 200 26");e.setAttribute("preserveAspectRatio","none");e.innerHTML=spark(TH[k],col)}
function toast(t){var e=$("toast");e.textContent=t;e.style.display="block";clearTimeout(TOASTT);TOASTT=setTimeout(function(){e.style.display="none"},8000)}
function doAct(p){$("res").className="muted";$("res").textContent="sending...";p.type="action";orca.postMessage(p)}
function num(id){var v=parseFloat($(id).value);return isNaN(v)?null:v}
var LS={},LNAMES=[],TD={};
function slv(e){$(e.id+"v").textContent=e.value+" %"}
function heaterOf(k){return k==="bed"?"heater_bed":(k==="chamber"?"heater_generic chamber":(LNAMES[parseInt(k)]||""))}
function setT(k){var v=num("ti"+k),mx=k==="bed"?120:(k==="chamber"?70:300);if(v===null||v<0||v>mx){$("res").className="err";$("res").textContent="temperature must be 0 to "+mx;return}
  TD[k]=0;doAct({kind:"temp",target:k,value:v})}
function syncHeat(s){["0","1","2","3","bed","chamber"].forEach(function(k){var h=s[heaterOf(k)]||{};var c=$("tc"+k);if(c)c.textContent=h.temperature===undefined?"-":t(h.temperature).replace(" °C","°");
  var e=$("ti"+k);if(e&&!TD[k]&&document.activeElement!==e&&h.target!==undefined)e.value=h.target>0?Math.round(h.target):""})}
function doFan(n,v){v=parseInt(v);if(isNaN(v)||v<0||v>100)return;doAct({kind:"fan",name:n,value:v})}
function doSpeed(v){v=parseInt(v);if(isNaN(v)||v<10||v>300)return;var e=$("sp");e.value=v;slv(e);doAct({kind:"speed",value:v})}
function syncCtl(s){var g=function(id,v){var e=$(id);if(e&&document.activeElement!==e&&v!==undefined&&v!==null){e.value=Math.round(v*100);slv(e)}};
  g("fp",(s.fan||{}).speed);g("fa",(s["fan_generic auxiliary_fan"]||{}).speed);g("fr",(s["fan_generic air_fan"]||{}).speed);g("sp",(s.gcode_move||{}).speed_factor)}
var HJOBS=[],HTOT=null,HERR="",HSEL={};
function fmtH(t){t=Math.round(t||0);var h=Math.floor(t/3600),m=Math.round((t%3600)/60);return h?h+" h "+m+" min":m+" min"}
function renderHist(){var tb="";
  if(HTOT){[["Prints",Math.round(HTOT.n)],["Print time",fmtH(HTOT.t)],["Filament",(HTOT.f/1000).toFixed(0)+" m"],["Longest print",fmtH(HTOT.l)]].forEach(function(x){tb+='<div style="flex:1;background:var(--card2);border-radius:10px;padding:8px 12px"><div class="muted" style="font-size:11px">'+x[0]+'</div><div style="font-size:17px;font-weight:700">'+x[1]+'</div></div>'})}
  $("htot").innerHTML=tb;var h="";
  HJOBS.forEach(function(j){var mt=FMETA[j.file]||{};var st=j.status,bc=st==="completed"?["rgba(0,174,66,.18)","#35d07f"]:(st==="in_progress"?["rgba(128,128,128,.2)","#aab"]:(st==="cancelled"?["rgba(224,160,48,.18)","#e0a030"]:["rgba(220,60,60,.18)","#ff7a75"]));
    var d=new Date(j.start*1000).toLocaleString()+" · "+fmtH(j.dur);var g=mt.g||j.g;if(g)d+=" · "+Math.round(g)+" g";else if(j.fu>0)d+=" · "+(j.fu/1000).toFixed(1)+" m";
    var th=mt.th?'<img class="fth" src="'+esc(mt.th)+'" onerror="this.style.visibility=\'hidden\'">':'<div class="fth"></div>';
    var cols=mt.c&&mt.c.length?mt.c:(j.c||[]);var dots=cols.map(function(c,i){var u=!mt.u||mt.u[i];return '<span style="display:inline-flex'+(u?"":";opacity:.3")+'">'+spool(c,20)+'</span>'}).join("");
    var btn=j.exists?'<button class="pri" data-ph="'+esc(j.file)+'" '+((STATE==="printing"||STATE==="paused")?"disabled":"")+'>Print again</button>':'<span class="muted" style="min-width:90px;text-align:center">file deleted</span>';
    h+='<div class="frow"><input type="checkbox" class="fchk" data-hc="'+esc(j.id)+'" '+(HSEL[j.id]?"checked":"")+'>'+th+'<div class="fmid"><div class="fname">'+esc(j.file)+'</div><div class="muted">'+d+'</div><div class="fdots">'+dots+'</div></div><span style="padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;background:'+bc[0]+';color:'+bc[1]+'">'+esc(st)+'</span>'+btn+'</div>'});
  $("hist").innerHTML=h||(HERR?"history not available: "+esc(HERR):"no jobs yet");
  Array.prototype.forEach.call($("hist").querySelectorAll("button[data-ph]"),function(b){b.onclick=function(){printFile(b.getAttribute("data-ph"))}});
  Array.prototype.forEach.call($("hist").querySelectorAll(".fchk"),function(c){c.onchange=function(){var k=c.getAttribute("data-hc");if(c.checked)HSEL[k]=1;else delete HSEL[k];updHDel()}});
  var keep={};HJOBS.forEach(function(j){if(HSEL[j.id])keep[j.id]=1});HSEL=keep;updHDel()}
function updHDel(){var n=Object.keys(HSEL).length;$("hDelBtn").textContent=n?"Delete ("+n+")":"Delete"}
function askDelHist(){var l=Object.keys(HSEL),r=$("res");if(!l.length){r.className="err";r.textContent="Tick the box next to each history entry you want to remove first.";return}
  var b=$("hdelbar");b.style.display="block";b.innerHTML='<div style="font-weight:700;margin-bottom:6px">Remove '+l.length+' entr'+(l.length>1?"ies":"y")+' from the history list?</div><div class="muted" style="margin-bottom:10px">Only the list entries go. The gcode files stay on the printer.</div><button id="hdYes" style="background:#c0392b;border-color:#c0392b;color:#fff">Yes, remove</button> <button id="hdNo">Cancel</button>';
  $("hdNo").onclick=function(){b.style.display="none"};
  $("hdYes").onclick=function(){var x=Object.keys(HSEL);HSEL={};b.style.display="none";updHDel();$("res").className="muted";$("res").textContent="removing...";orca.postMessage({type:"deletehist",ids:x})}}
function loadTL(){TLLOADED=true;$("tllist").textContent="loading...";orca.postMessage({type:"timelapse"})}
function updTDel(){var n=Object.keys(TSEL).length;$("tDelBtn").textContent=n?"Delete ("+n+")":"Delete"}
function renderTL(){var h="";TLV.forEach(function(v){var d=new Date(v.modified*1000).toLocaleDateString()+" · "+(v.size/1048576).toFixed(1)+" MB";
  var th=v.th?'<img src="'+esc(v.th)+'" style="width:100%;height:110px;object-fit:cover;display:block" onerror="this.style.visibility=\'hidden\'">':'<div style="height:110px"></div>';
  h+='<div style="border:1px solid var(--keyb);border-radius:12px;overflow:hidden;background:var(--card2,#1d2128)"><div style="position:relative;background:#000">'+th+'<input type="checkbox" class="fchk" data-t="'+esc(v.path)+'" '+(TSEL[v.path]?"checked":"")+' style="position:absolute;top:8px;left:8px"></div><div style="padding:10px"><div class="fname" style="font-weight:600;word-break:break-all">'+esc(v.path)+'</div><div class="muted" style="margin:2px 0 8px">'+d+'</div><div style="display:flex;gap:8px"><button class="pri" style="flex:1" data-p="'+esc(v.path)+'">Play</button><button data-o="'+esc(v.path)+'" title="Download this video">Download</button></div></div></div>'});
  $("tllist").innerHTML=h||(TLERR?"timelapse not available: "+esc(TLERR):"no timelapse videos yet");
  $("tlcount").textContent=TLV.length?TLV.length+" video"+(TLV.length==1?"":"s"):"";
  var L=$("tllist");
  Array.prototype.forEach.call(L.querySelectorAll("button[data-p]"),function(b){b.onclick=function(){playTL(b.getAttribute("data-p"))}});
  Array.prototype.forEach.call(L.querySelectorAll("button[data-o]"),function(b){b.onclick=function(){orca.postMessage({type:"opentl",file:b.getAttribute("data-o")})}});
  Array.prototype.forEach.call(L.querySelectorAll(".fchk"),function(c){c.onchange=function(){var k=c.getAttribute("data-t");if(c.checked)TSEL[k]=1;else delete TSEL[k];updTDel()}});
  var keep={};TLV.forEach(function(v){if(TSEL[v.path])keep[v.path]=1});TSEL=keep;updTDel()}
function playTL(p){var v=TLV.filter(function(x){return x.path===p})[0];if(!v)return;$("tlplay").style.display="block";$("tlname").textContent=p;var e=$("tlvid");e.src=v.url;e.load();e.play().catch(function(){})}
function closeTL(){var e=$("tlvid");e.pause();e.removeAttribute("src");e.load();$("tlplay").style.display="none"}
function askDelTL(){var x=Object.keys(TSEL),b=$("tdelbar");if(!x.length){$("res").className="err";$("res").textContent="tick the videos to delete first";return}
  b.style.display="block";b.innerHTML='<div style="font-weight:700;margin-bottom:6px">Delete '+x.length+' timelapse video'+(x.length==1?"":"s")+' from the printer? This cannot be undone.</div><div class="muted" style="margin-bottom:8px">'+x.map(esc).join(", ")+'</div><button id="tdYes" style="background:#c0392b;border-color:#c0392b;color:#fff">Yes, delete</button> <button id="tdNo">Cancel</button>';
  $("tdNo").onclick=function(){b.style.display="none"};
  $("tdYes").onclick=function(){var y=Object.keys(TSEL);TSEL={};b.style.display="none";updTDel();closeTL();$("res").className="muted";$("res").textContent="deleting...";orca.postMessage({type:"deletetl",files:y})}}
function loadHist(){$("hist").textContent="loading...";orca.postMessage({type:"history"})}
setInterval(function(){try{orca.postMessage({type:"ping"})}catch(e){}},3000);
reqFils();setInterval(reqFils,6000);
function addOpt(a){if(!a)return;var s=$("preset");for(var i=0;i<s.options.length;i++){if(s.options[i].value===a)return}var o=document.createElement("option");o.value=a;o.textContent=a;s.insertBefore(o,s.options[s.options.length-1])}
function connect(){FIRST=true;var a=$("addr").value.trim();addOpt(a);syncPreset(a);try{localStorage.setItem("wmdev_addr",a);var L=JSON.parse(localStorage.getItem("wmdev_addrs")||"[]");if(a&&L.indexOf(a)<0){L.push(a);localStorage.setItem("wmdev_addrs",JSON.stringify(L.slice(-8)))}}catch(e){}
  $("conn").textContent="connecting...";orca.postMessage({type:"connect",addr:a})}
var THN=["","a","b"],THL=["Current","A bento graphite","B soft slate"];
function setTheme(i){document.body.setAttribute("data-th",THN[i]);var bt=$("thBtn");if(bt)bt.textContent="Style: "+THL[i];try{localStorage.setItem("wmth",String(i))}catch(e){}}
function cycleTheme(){var i=0;try{i=parseInt(localStorage.getItem("wmth")||"0")}catch(e){}if(!(i>=0))i=0;setTheme((i+1)%3)}
(function(){var i=0;try{i=parseInt(localStorage.getItem("wmth")||"0")}catch(e){}if(i>0)setTimeout(function(){setTheme(i)},0)})();
function toggleCam(){camOn=!camOn;$("camBox").style.display=camOn?"block":"none";$("camBtn").textContent=camOn?"Hide camera":"Show camera";setCam()}
function refreshCam(){if(!camOn)toggleCam();var img=$("cam");img.removeAttribute("src");CAMBUST=Date.now();setTimeout(setCam,200)}
var CAMBUST=0;
function setCam(){var img=$("cam");if(!camOn||!BASE){img.removeAttribute("src");return}
  var u=(CAMS[0]&&CAMS[0].stream_url)||"/webcam/?action=stream";var host=BASE.replace(/:\d+$/,"");
  var url=u.indexOf("http")==0?u:host+u;if(CAMBUST)url+=(url.indexOf("?")>=0?"&":"?")+"_t="+CAMBUST;$("camMsg").textContent=url;img.onerror=function(){$("camMsg").textContent="camera did not load: "+url};img.src=url}
var SPM={1:"PLA",2:"PLA+",3:"PLA Silk",4:"PLA Matte",5:"PLA-CF",6:"PETG",7:"PETG-CF",8:"ABS",9:"ASA",10:"TPU",11:"PA",12:"PC",13:"PVA",14:"HIPS",15:"PLA Metal"};
var SPC=[["White","FFFFFF"],["Beige","F3E8C8"],["Brown","9A6738"],["Grey","C7D5DF"],["Black","000000"],["Cyan","00DDE3"],["Light blue","13A7E8"],["Dark blue","0B4BD8"],["Light green","16E900"],["Dark green","00A84F"],["Yellow","FFF200"],["Orange","FF5A1F"],["Light pink","DDBFDE"],["Magenta","E72DBE"],["Red","FF1C2E"],["Bright blue","006EFF"],["Gold","D9A520"],["Copper","B86B44"],["Silver","AFC0CC"],["Multicolour","C060C0"],["Clear","D5E8EE"]];
var ZRM=["PLA","PLA Silk","ABS","PETG","PLA Matte","PLA Metal","ABS Matte","PETG Matte","PVA","TPE","Marble","HIPS","TPU","PET","Wood","ASA","PLA+","PLA-CF","PETG-CF","PA","PA-CF","PC","ABS-CF","PA-GF","PC-ABS","PP","BVOH"];
var ZRC=[1,3,8,6,4,15,8,6,13,10,15,14,10,15,15,9,2,5,7,11,11,12,8,11,12,15,10];
var ZRV=false;
var ZRT=[210,215,250,240,210,215,250,240,200,225,210,235,225,240,200,255,215,220,250,270,280,270,260,280,260,230,210];
function spMat(){if(ZRV)return;var mi=parseInt($("spM").value);$("spN").value=mi>=0?ZRT[mi]:""}
var SPI=0,SLOTS=[];
var HSV=[0,1,1];
function hsv2hex(h,sa,v){var c=v*sa,x=c*(1-Math.abs((h/60)%2-1)),m=v-c,r=0,g=0,b=0;
  if(h<60){r=c;g=x}else if(h<120){r=x;g=c}else if(h<180){g=c;b=x}else if(h<240){g=x;b=c}else if(h<300){r=x;b=c}else{r=c;b=x}
  return [r+m,g+m,b+m].map(function(z){var n=Math.round(z*255).toString(16);return (n.length<2?"0":"")+n}).join("").toUpperCase()}
function hex2hsv(hx){var n=parseInt(hx,16),r=(n>>16)/255,g=((n>>8)&255)/255,b=(n&255)/255,mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn,h=0;
  if(d){if(mx===r)h=60*(((g-b)/d+6)%6);else if(mx===g)h=60*((b-r)/d+2);else h=60*((r-g)/d+4)}
  return [h,mx?d/mx:0,mx]}
function showNear(){var v=$("spH").value.replace("#","");if(!/^[0-9A-Fa-f]{6}$/.test(v)){$("spNear").textContent="";return}
  var n=parseInt(v,16),r=n>>16,g=(n>>8)&255,b=n&255,best="",bd=1e12;SPC.forEach(function(p){var q=parseInt(p[1],16),cr=q>>16,cg=(q>>8)&255,cb=q&255,rm=(r+cr)/2,d=(2+rm/256)*(r-cr)*(r-cr)+4*(g-cg)*(g-cg)+(2+(255-rm)/256)*(b-cb)*(b-cb);if(d<bd){bd=d;best=p[0]}});
  $("spNear").textContent="The printer screen and Fluidd keep a fixed list of 21 colours and will show: "+best}
function drawPick(){$("svBox").style.background="linear-gradient(to top,#000,rgba(0,0,0,0)),linear-gradient(to right,#fff,hsl("+Math.round(HSV[0])+",100%,50%))";
  var k=$("svK");k.style.left=(HSV[1]*100)+"%";k.style.top=((1-HSV[2])*100)+"%";$("hue").value=Math.round(HSV[0]);showNear()}
function setHex(h){h=String(h).replace("#","").toUpperCase();$("spH").value=h;$("spW").style.background="#"+h;HSV=hex2hsv(h);drawPick()}
function hexIn(v){v=v.replace("#","").toUpperCase();if(/^[0-9A-F]{6}$/.test(v)){$("spW").style.background="#"+v;HSV=hex2hsv(v);drawPick()}}
function hueIn(v){HSV[0]=parseFloat(v);var hx=hsv2hex(HSV[0],HSV[1],HSV[2]);$("spH").value=hx;$("spW").style.background="#"+hx;drawPick()}
function svAt(ev){var r=$("svBox").getBoundingClientRect();HSV[1]=Math.min(1,Math.max(0,(ev.clientX-r.left)/r.width));HSV[2]=1-Math.min(1,Math.max(0,(ev.clientY-r.top)/r.height));
  var hx=hsv2hex(HSV[0],HSV[1],HSV[2]);$("spH").value=hx;$("spW").style.background="#"+hx;drawPick()}
(function(){var dn=false,b=$("svBox");b.addEventListener("mousedown",function(e){dn=true;svAt(e);e.preventDefault()});
  window.addEventListener("mousemove",function(e){if(dn)svAt(e)});window.addEventListener("mouseup",function(){dn=false})})();
function editSpool(i){SPI=i;var k=SLOTS[i]||{};$("spT").textContent="Toolhead "+(i+1);
  var top=ZRV?ZRM.length:16;var o='<option value="-1">Empty / not set</option>';for(var m=0;m<top;m++)o+='<option value="'+m+'">'+ZRM[m]+'</option>';$("spM").innerHTML=o;
  var mi=(typeof k.midx==="number")?k.midx:-1;if(mi>=top)mi=-1;if(ZRV&&!k.mat)mi=-1;$("spM").value=String(mi);
  $("spN").readOnly=!ZRV;$("spNL").textContent=ZRV?"Nozzle temp (blank = none)":"Nozzle temp (material default, this printer stores no temperature)";$("spM").onchange=spMat;
  if(ZRV)$("spN").value=k.temp>0?k.temp:"";else spMat();
  var c="";SPC.forEach(function(p){c+='<i class="rsw" title="'+p[0]+'" style="background:#'+p[1]+'" onclick="setHex(\''+p[1]+'\')"></i>'});$("spC").innerHTML=c;
  setHex((k.colour||"#FFFFFF").replace("#",""));$("spool").style.display="flex"}
function saveSpool(){var hx=$("spH").value.replace("#","").toUpperCase(),tp=$("spN").value===""?0:parseInt($("spN").value);
  if(!/^[0-9A-F]{6}$/.test(hx)){toast("colour must be 6 hex digits, e.g. FF1C2E");return}
  if(isNaN(tp)||!(tp===0||(tp>=150&&tp<=300))){toast("temperature: blank or 150 to 300");return}
  var mi=parseInt($("spM").value),m=mi<0?0:ZRC[mi];
  $("spool").style.display="none";$("res").className="muted";$("res").textContent="sending...";orca.postMessage({type:"action",kind:"spool",t:SPI,mat:m,midx:mi,hex:hx,temp:tp})}
function hideConfirm(){PENDING=null;$("confirm").style.display="none"}
function showConfirm(text,fn){PENDING=fn;$("confirmText").textContent=text;$("confirm").style.display="block"}
$("confirmYes").onclick=function(){var f=PENDING;hideConfirm();if(f)f()};
function openInstallHelper(){$("helperModalResult").textContent="";$("helperResult").textContent="";$("helperOverlay").style.display="flex"}
function closeInstallHelper(){$("helperOverlay").style.display="none"}
function confirmInstallHelper(){
  var user=($("helperUser").value||"").trim(),pass=$("helperPass").value||"";
  if(!user||!pass){$("helperModalResult").className="err";$("helperModalResult").textContent="Enter a username and password first";return}
  $("helperModalResult").textContent="";
  $("helperGo").disabled=true;$("helperGo").textContent="Installing...";
  orca.postMessage({type:"install_helper",user:user,password:pass})}
function onInstallHelperResult(ok,detail){
  $("helperGo").disabled=false;$("helperGo").textContent="Install";
  if(ok){
    closeInstallHelper();HELPEROK=true;var hc=$("helperCard");if(hc)hc.style.display="none";
    $("helperResult").className="ok";$("helperResult").textContent=detail||"Installed."}
  else{$("helperModalResult").className="err";$("helperModalResult").textContent=detail||"Install failed"}}
function ask(cmd){
  if(cmd==="cancel")showConfirm("Cancel the running print? This cannot be undone.",function(){send(cmd)});
  else send(cmd)}
function send(cmd,file){$("res").className="muted";$("res").textContent="sending "+cmd+"...";orca.postMessage({type:"command",name:cmd,file:file||""})}
function loadFiles(){$("files").textContent="loading...";orca.postMessage({type:"files"})}
function printFile(f){$("res").className="muted";$("res").textContent="opening the mapping window...";orca.postMessage({type:"printfile",file:f})}
var FROWS=[],FMETA={},SEL={};
function fmtDur(t){var h=Math.floor(t/3600),m=Math.round((t%3600)/60);return (h?h+"h ":"")+m+"m"}
function tgl(p){if(!p)return"";var tg=!p.filament_detected;return '<div class="tt"><span class="dot '+(tg?"red":"off")+'"></span>'+(tg?"TANGLE":"tangle")+'</div>'}
function renderFiles(){var h="";FROWS.forEach(function(f){var mt=FMETA[f.path]||{};var d=new Date(f.modified*1000).toLocaleDateString()+" · "+(f.size/1048576).toFixed(1)+" MB";
  if(mt.h)d+=" · "+fmtDur(mt.h);if(mt.g)d+=" · "+Math.round(mt.g)+" g";
  var th=mt.th?'<img class="fth" src="'+esc(mt.th)+'" onerror="this.style.visibility=\'hidden\'">':'<div class="fth"></div>';
  var dots=(mt.c||[]).map(function(c,i){var u=!mt.u||mt.u[i];return '<span style="display:inline-flex'+(u?"":";opacity:.3")+'">'+spool(c,20)+'</span>'}).join("");
  h+='<div class="frow"><input type="checkbox" class="fchk" data-c="'+esc(f.path)+'" '+(SEL[f.path]?"checked":"")+'>'+th+'<div class="fmid"><div class="fname">'+esc(f.path)+'</div><div class="muted">'+d+'</div><div class="fdots">'+dots+'</div></div><button class="pri" '+((STATE==="printing"||STATE==="paused")?"disabled":"")+' data-f="'+esc(f.path)+'">Print</button></div>'});
  $("files").innerHTML=h||"no files in gcodes";
  Array.prototype.forEach.call($("files").querySelectorAll("button"),function(b){b.onclick=function(){printFile(b.getAttribute("data-f"))}});
  Array.prototype.forEach.call($("files").querySelectorAll(".fchk"),function(c){c.onchange=function(){var k=c.getAttribute("data-c");if(c.checked)SEL[k]=1;else delete SEL[k];updDel()}});
  var keep={};FROWS.forEach(function(f){if(SEL[f.path])keep[f.path]=1});SEL=keep;updDel()}
function showPrompt(p){var h='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font-size:18px;font-weight:700">'+esc(p.title||"Printer")+'</span><span style="cursor:pointer;font-size:20px;color:var(--orca-muted)" onclick="$(\'pmodal\').style.display=\'none\'">&times;</span></div>',row=[],last=-2,foot="";
  function flush(){if(row.length){h+='<div style="display:flex;gap:10px;margin-bottom:10px">'+row.join("")+'</div>';row=[]}}
  (p.items||[]).forEach(function(it,ix){
    if(it.t==="text"){flush();h+='<div style="font-size:15px;font-weight:600;line-height:1.5;margin:0 0 14px">'+esc(it.text)+'</div>';return}
    var bg=it.color==="primary"?"var(--g)":(it.color==="error"||it.color==="warning")?"#c0392b":"var(--key)",fg=it.color==="primary"||it.color==="error"||it.color==="warning"?"#fff":"var(--keyt)";
    var b='<button data-px="'+ix+'" style="flex:1;padding:14px 8px;font-size:15px;font-weight:700;border-radius:10px;background:'+bg+';color:'+fg+'">'+esc(it.label)+'</button>';
    if(it.grp===-1){foot+=b;return}
    if(it.grp!==last){flush();last=it.grp}row.push(b)});
  flush();if(foot)h+='<div style="display:flex;gap:10px">'+foot+'</div>';
  var box=$("pbox");box.innerHTML=h;$("pmodal").style.display="flex";
  Array.prototype.forEach.call(box.querySelectorAll("button[data-px]"),function(b){b.onclick=function(){var it=p.items[b.getAttribute("data-px")];$("pmodal").style.display="none";orca.postMessage({type:"promptbtn",script:it.script||""})}})}
function selList(){return Object.keys(SEL)}
function updDel(){var n=selList().length;$("delBtn").textContent=n?"Delete ("+n+")":"Delete"}
function askDelete(){var l=selList(),r=$("res");if(!l.length){r.className="err";r.textContent="Tick the box next to the picture of each file you want to delete first.";return}
  if(STATE==="printing"||STATE==="paused"){r.className="err";r.textContent="Files cannot be deleted while a print is running.";return}
  var nm=l.slice(0,5).map(function(x){return esc(x)}).join("<br>")+(l.length>5?"<br>and "+(l.length-5)+" more":"");
  var b=$("delbar");b.style.display="block";b.innerHTML='<div style="font-weight:700;margin-bottom:6px">Delete '+l.length+' file'+(l.length>1?"s":"")+' from the printer? This cannot be undone.</div><div class="muted" style="margin-bottom:10px">'+nm+'</div><button id="delYes" style="background:#c0392b;border-color:#c0392b;color:#fff">Yes, delete</button> <button id="delNo">Cancel</button>';
  $("delNo").onclick=function(){b.style.display="none"};
  $("delYes").onclick=function(){var x=selList();SEL={};b.style.display="none";updDel();$("res").className="muted";$("res").textContent="deleting...";orca.postMessage({type:"deletefiles",files:x})}}
function ink(c){var m=/^#?([0-9a-f]{6})$/i.exec(c||"");if(!m)return "#fff";var n=parseInt(m[1],16),r=n>>16,g=(n>>8)&255,b=n&255;return (0.299*r+0.587*g+0.114*b)>150?"#111":"#fff"}
function wedge(a0,a1,r1,r2){function q(r,a){return (r*Math.cos(a)).toFixed(2)+" "+(r*Math.sin(a)).toFixed(2)}return "M"+q(r2,a0)+" A"+r2+" "+r2+" 0 0 1 "+q(r2,a1)+" L"+q(r1,a1)+" A"+r1+" "+r1+" 0 0 0 "+q(r1,a0)+" Z"}
function spool(c,z){c=esc(c||"#808080");z=z||20;var k="rgba(0,0,0,.6)",s='<svg viewBox="-50 -50 100 100" width="'+z+'" height="'+z+'" style="flex:none;vertical-align:middle"><circle r="47" fill="'+c+'" stroke="'+k+'" stroke-width="4"/>';for(var i=0;i<6;i++){var m=i*Math.PI/3-Math.PI/2;s+='<path d="'+wedge(m-.36,m+.36,20,38)+'" fill="#1b1d20" stroke="'+k+'" stroke-width="3" stroke-linejoin="round"/>'}return s+'<circle r="13" fill="'+c+'" stroke="'+k+'" stroke-width="4"/><circle r="6" fill="#1b1d20" stroke="'+k+'" stroke-width="2"/></svg>'}
var TABS=["status","files","hist","tl","msgs"],TLV=[],TSEL={},TLERR="",TLLOADED=false;
function showTab(n){if(n==="tl"&&!TLLOADED)loadTL();TABS.forEach(function(k){$("v-"+k).style.display=k===n?"block":"none";$("n-"+k).className="ni"+(k===n?" sel":"")})}
var ARMT=null,STEP=10;

function mvState(){}
function setStep(n){STEP=n;var sp=$("mvStep").children;for(var i=0;i<sp.length;i++)sp[i].className=(parseInt(sp[i].textContent)===n)?"on":""}
function dryGo(on){
  var tp=parseInt($("dryTemp").value),du=parseInt($("dryDur").value);
  if(on&&(!(tp>=35&&tp<=65)||!(du>=10&&du<=720))){toast("Dryer: 35 to 65 C, 10 to 720 minutes");return}
  $("res").className="muted";$("res").textContent="sending...";orca.postMessage({type:"action",kind:"dry",on:on,temp:tp,dur:du})}
function renderAce(s){var a=s.ace_instance_0,st=s.ace||{};var box=$("aceBox");if(!a){box.style.display="none";return}
  box.style.display="block";var cs=String(a.connection_state||""),ok=(!cs||/^(connected|ok|ready|online)$/i.test(cs));
  $("aceDot").className="dot "+(ok?"on":"red");$("aceTitle").textContent="ACE Pro 1 - "+(ok?"Connected":"Not connected")+(a.status?" ("+a.status+")":"");
  var ev=((s.save_variables||{}).variables||{}).wm_endless;$("aceEs").textContent="Endless spool: "+((ev!=null?Number(ev)===1:st.endless_spool_enabled)?"On":"Off");
  var w=$("aceWarn");if(!ok){w.style.display="block";w.textContent="ACE is not connected (state: "+cs+"). If a print is running, check the USB cable and power.";}else w.style.display="none";
  var d=a.dryer||{};
  $("dryS").textContent=d.status||"-";$("dryT").textContent=(a.temp!=null?t(a.temp):"-")+" / "+(d.target_temp>0?t(d.target_temp):"off");$("dryR").textContent=d.remain_time>0?(Math.floor(d.remain_time/60)+" h "+(d.remain_time%60)+" min"):"-";
  var tm=st.tool_map||{},keys=Object.keys(tm);$("aceMap").textContent=keys.length?keys.map(function(x){return x+"→"+tm[x]}).join(", "):"default (slots 1-4)";
  var q=st.stats||{};$("aceTc").textContent=q.tool_changes!=null?q.tool_changes:"-";$("aceFl").textContent=q.purge_mm!=null?(q.purge_mm.toFixed(1)+" mm"):"-";$("aceCt").textContent=q.cuts!=null?q.cuts:"-"}

var SWT={};
function aceSw(n,on){SWT["a"+n]=Date.now();act({kind:"acepin",name:n,on:on})}
function endSw(n,on){SWT["e"+n]=Date.now();act({kind:"endless",name:n,on:on})}
var ENDOFF=null;
function endGo(){if(ENDOFF&&ENDOFF.to!=null)act({kind:"endless_go",frm:ENDOFF.from,to:ENDOFF.to})}
function setSw(id,key,v,dis){var e=$(id);if(!e)return;if(!SWT[key]||Date.now()-SWT[key]>4000)e.checked=!!v;e.disabled=!!dis}
function renderFil(s,sl){
  var vars=((s.save_variables||{}).variables)||{};
  var busy=(STATE==="printing"||STATE==="paused");
  var bu=$("btnUnload");if(bu)bu.style.display=s["gcode_macro WM_UNLOAD_MENU"]?"":"none";var bl=$("btnLight");if(bl){bl.style.display=(s["gcode_macro WM_CHAMBER_LIGHT"]||HASTMT1)?"":"none";bl.classList.toggle("on",LIGHTON==="on")}
  var hc=$("helperCard");if(hc)hc.style.display=(HASTMT1&&!HELPEROK)?"block":"none";
  var hasAce=!!(s["gcode_macro _WM_ACE"]||s.ace||s.ace_instance_0);
  var pro=s["output_pin ACE_Pro"],aut=s["output_pin ACE_Auto_Load"];
  var box=$("aceSw");box.style.display=(hasAce&&(pro||aut))?"block":"none";
  $("rowPro").style.display=pro?"flex":"none";$("rowAuto").style.display=aut?"flex":"none";
  if(pro){var on=Number(pro.value)>=0.5;setSw("swPro","apro",on,busy);$("proHint").textContent=on?"On: the printer uses the ACE Pro for tool changes and slot moves":"Off: the printer ignores tool changes and slot moves"}
  if(aut)setSw("swAuto","aauto",Number(aut.value)>=0.5,false);
  var eb=$("endBox"),sens=!!s["filament_switch_sensor filament0"],has=!!s["gcode_macro _WM_ENDLESS_CONTINUE"]&&sens;
  $("fil2").style.display=sens?"grid":"none";if(sens)mapUpdate(s,sl,vars);else $("mapBox").style.display="none";
  eb.style.display=has?"block":"none";if(!has)return;
  setSw("swEnd","eenable",Number(vars.wm_endless||0)===1,false);
  var auto=Number(vars.wm_endless_auto==null?1:vars.wm_endless_auto)===1;
  setSw("swEAuto","eauto",auto,false);
  setSw("swETang","etangle",Number(vars.wm_endless_tangle==null?1:vars.wm_endless_tangle)===1,false);
  var W=s["gcode_macro _WM_ENDLESS"]||{};
  var out=[0,1,2,3].map(function(i){return Number(W["out"+i]||0)!==0});
  var keys=[0,1,2,3].map(function(i){var m=vars["zr_t"+i+"_mat"],c=vars["zr_t"+i+"_color"];return(m==null||c==null)?null:m+"/"+c});
  var loaded=[0,1,2,3].map(function(i){var f=s["filament_switch_sensor filament"+i];return f?f.filament_detected:null});
  var ai=s.ace_instance_0,aceOn=hasAce&&ai&&(ai.connection_state==null||ai.connection_state==="connected")&&vars.ace_global_enabled!==false;
  var aslots=(ai&&ai.slots)||[];
  var ready=[0,1,2,3].map(function(i){return loaded[i]===true&&!out[i]&&!(aceOn&&aslots[i]&&aslots[i].status==="empty")});
  var ce=vars.current_extruder,act0=-1;
  if(ce!=null&&Number(ce)>=0&&Number(ce)<4)act0=Number(ce);else{var te=(s.toolhead||{}).extruder||"";act0=te==="extruder"?0:(/^extruder(\d)$/.test(te)?Number(RegExp.$1):-1)}
  var run=busy?act0:-1;
  var sets=[];keys.forEach(function(k,i){if(k==null||sets.some(function(x){return x.indexOf(i)>=0}))return;var m=[];keys.forEach(function(k2,j){if(k2===k)m.push(j)});if(m.length>1)sets.push(m)});
  function nextOf(e){var r=[];if(keys[e]==null)return r;for(var a=1;a<4;a++){var o=(e+a)%4;if(keys[o]===keys[e]&&ready[o])r.push(o)}return r}
  var T=function(i){return"T"+(i+1)},cx=function(i){return(sl[i]&&sl[i].colour)||"#808080"},mn=function(i){return(sl[i]&&sl[i].material)||""};
  var P=function(a,r){return[52+r*Math.sin(a),52-r*Math.cos(a)]},R=function(n){return Math.round(n*100)/100};
  var g="";
  for(var i=0;i<4;i++){var a=i*Math.PI/2+.04,o=(i+1)*Math.PI/2-.04,p1=P(a,50),p2=P(o,50),p3=P(o,31),p4=P(a,31),tp=P((a+o)/2,40.5);
    var dim=!ready[i]&&i!==run;
    g+='<path d="M'+R(p1[0])+' '+R(p1[1])+'A50 50 0 0 1 '+R(p2[0])+' '+R(p2[1])+'L'+R(p3[0])+' '+R(p3[1])+'A31 31 0 0 0 '+R(p4[0])+' '+R(p4[1])+'Z" fill="'+esc(cx(i))+'" opacity="'+(dim?.4:1)+'"'+(i===run?' stroke="#fff" stroke-width="2"':'')+'/><text x="'+R(tp[0])+'" y="'+R(tp[1]+4.5)+'" text-anchor="middle" font-size="12" font-weight="700" fill="'+ink(cx(i))+'">'+T(i)+'</text>'}
  sets.forEach(function(st){for(var r=0;r+1<st.length;r++){var q1=P((st[r]+.5)*Math.PI/2,24),q2=P((st[r+1]+.5)*Math.PI/2,24);
    g+='<path d="M'+R(q1[0])+' '+R(q1[1])+'A24 24 0 0 1 '+R(q2[0])+' '+R(q2[1])+'" fill="none" stroke="#3fbf63" stroke-width="3" stroke-linecap="round"/><circle cx="'+R(q1[0])+'" cy="'+R(q1[1])+'" r="3" fill="#3fbf63"/><circle cx="'+R(q2[0])+'" cy="'+R(q2[1])+'" r="3" fill="#3fbf63"/>'}});
  $("endRing").innerHTML=g;
  var chips=[],swapOn=busy&&Number(W.from)>=0&&Number(W.to)===run&&String(W.swap_file||"")===String((s.print_stats||{}).filename||"");
  sets.forEach(function(st){var col=cx(st[0]),mat=mn(st[0]),others=st.filter(function(x){return x!==run});
    if(st.indexOf(run)>=0){var nx=nextOf(run);
      if(loaded[run]===false&&nx.length===0){chips.push({h:st.map(T).join(", "),m:"",n:"no toolhead ready, print paused",k:"wn",c:col,f:1});return}
      chips.push({h:swapOn?T(Number(W.from))+" → "+T(run):T(run),m:"",n:nx.length?("printing, next: "+nx.map(T).join(", then ")):"printing, no spare left",k:nx.length?"ok":"wn",c:col,f:0})}
    else{var rd=st.filter(function(x){return ready[x]});
      if(rd.length>1)chips.push({h:rd.map(T).join(rd.length===2?" ⇄ ":" → "),m:mat,n:"identical",k:"ok",c:col,f:0});
      else if(rd.length===1)chips.push({h:T(rd[0]),m:mat,n:"no spare left",k:"wn",c:col,f:0})}
    var fo=others.filter(function(x){return out[x]}),em=others.filter(function(x){return!out[x]&&loaded[x]===false}),ae=others.filter(function(x){return!out[x]&&loaded[x]===true&&!ready[x]});
    if(fo.length)chips.push({h:fo.map(T).join(", "),m:"",n:"tangle or spool end, check",k:"wn",c:col,f:1});
    if(em.length)chips.push({h:em.map(T).join(", "),m:"",n:"empty, refill",k:"wn",c:col,f:1});
    if(ae.length)chips.push({h:ae.map(T).join(", "),m:"",n:"spool empty at the ACE, refill",k:"wn",c:col,f:1})});
  for(var j=0;j<4;j++)if(!sets.some(function(x){return x.indexOf(j)>=0}))chips.push({h:T(j),m:"",n:"no spare",k:"ok",c:cx(j),f:0});
  $("endChips").innerHTML=chips.map(function(c){return'<span class="chip'+(c.f?' fd':'')+'"><span class="cd" style="background:'+esc(c.c)+'"></span><b>'+esc(c.h)+'</b>'+(c.m?'<span class="mt">'+esc(c.m)+'</span>':'')+'<span class="'+c.k+'">'+esc(c.n)+'</span></span>'}).join("");
  var off=null;
  if(Number(vars.wm_endless||0)===1&&STATE==="paused"&&act0>=0&&act0<4&&loaded[act0]===false){
    var n0=nextOf(act0)[0];
    if(n0==null)off={text:"Toolhead "+(act0+1)+" is empty and no toolhead with the same material and colour has filament. Load filament, then resume.",from:act0,to:null};
    else if(!auto)off={text:"Toolhead "+(act0+1)+" is empty. Toolhead "+(n0+1)+" has the same material and colour ("+mn(n0)+").",from:act0,to:n0}}
  ENDOFF=off;var ob=$("endOffer");ob.style.display=off?"flex":"none";
  if(off){$("endOfferTxt").textContent=off.text;$("endGo").style.display=off.to!=null?"":"none"}
}

var PALN=[["White", "#FFFFFF"], ["Beige", "#F3E8C8"], ["Brown", "#9A6738"], ["Grey", "#C7D5DF"], ["Black", "#000000"], ["Cyan", "#00DDE3"], ["Light blue", "#13A7E8"], ["Dark blue", "#0B4BD8"], ["Light green", "#16E900"], ["Dark green", "#00A84F"], ["Yellow", "#FFF200"], ["Orange", "#FF5A1F"], ["Light pink", "#DDBFDE"], ["Magenta", "#E72DBE"], ["Red", "#FF1C2E"], ["Bright blue", "#006EFF"], ["Gold", "#D9A520"], ["Copper", "#B86B44"], ["Silver", "#AFC0CC"], ["Multicolour", "#C060C0"], ["Clear", "#D5E8EE"]];
function hx(c){var m=/^#?([0-9a-f]{6})/i.exec(c||"");return m?[parseInt(m[1].substr(0,2),16),parseInt(m[1].substr(2,2),16),parseInt(m[1].substr(4,2),16)]:null}
function cdist(a,b){var x=hx(a),y=hx(b);if(!x||!y)return 400;var rm=(x[0]+y[0])/2,dr=x[0]-y[0],dg=x[1]-y[1],db=x[2]-y[2];return Math.sqrt((2+rm/256)*dr*dr+4*dg*dg+(2+(255-rm)/256)*db*db)}
function cname(c){var b="",bd=1e9;PALN.forEach(function(p){var d=cdist(c,p[1]);if(d<bd){bd=d;b=p[0]}});return b}
function sameMat(a,b){a=String(a||"").toUpperCase().trim();b=String(b||"").toUpperCase().trim();return !!a&&!!b&&(a===b||b.indexOf(a)===0||a.indexOf(b)===0)}
var FILS=[],FILERR="",MAP=[0,1,2,3],MHEADS=[];
function reqFils(){try{orca.postMessage({type:"orcafils"})}catch(e){}}
function mapRows(){return FILS.length?FILS.slice(0,4):MHEADS.map(function(h,i){return{index:i,colour:h.colour,type:h.mat,name:""}})}
function renderMap(){
  var rows=mapRows(),box=$("mapBox");if(!MHEADS.length){box.style.display="none";return}box.style.display="block";
  $("mSrc").textContent=FILS.length?("Filaments of the open Orca project ("+FILS.length+(FILS.length>4?", only the first 4 are shown":"")+"), and the toolhead each one prints on."):("Orca project filaments not available"+(FILERR?" ("+FILERR+")":"")+": showing the printer's own four tools.");
  var W=600,RH=44,GAP=8,H=Math.max(rows.length,MHEADS.length)*(RH+GAP),CW=232,X2=W-CW,svg='<svg viewBox="0 0 '+W+' '+(H+4)+'" width="100%" style="max-width:640px;display:block;margin:6px auto 0;overflow:visible">',warn=[],lines="",cards="";
  rows.forEach(function(f,i){var y=i*(RH+GAP)+2,t=Math.max(0,Math.min(3,Number(MAP[i])||0)),hd=MHEADS.filter(function(x){return x.i===t})[0]||{},ok=hd.loaded!==false&&sameMat(f.type,hd.mat)&&cdist(f.colour,hd.colour)<=300,y2=t*(RH+GAP)+2,col=f.colour||"#808080";
    if(!ok)warn.push(hd.loaded===false?"Toolhead "+(t+1)+" is empty: load filament before you print filament "+(i+1)+".":"Filament "+(i+1)+" ("+cname(f.colour)+" "+(f.type||"")+") prints on toolhead "+(t+1)+" ("+(hd.colour?cname(hd.colour):"no colour")+" "+(hd.mat||"")+"): the colours or materials differ.");
    lines+='<path d="M'+CW+' '+(y+RH/2)+' C'+(CW+70)+' '+(y+RH/2)+' '+(X2-70)+' '+(y2+RH/2)+' '+X2+' '+(y2+RH/2)+'" fill="none" stroke="'+esc(col)+'" stroke-width="3.5" stroke-linecap="round"'+(ok?'':' stroke-dasharray="1 8"')+'/>';
    cards+='<rect x="0" y="'+y+'" width="'+CW+'" height="'+RH+'" rx="9" fill="var(--hd)" stroke="var(--orca-border)"/><circle cx="24" cy="'+(y+RH/2)+'" r="11" fill="'+esc(col)+'" stroke="rgba(128,128,128,.7)"/><text x="46" y="'+(y+19)+'" font-size="14" font-weight="700" fill="currentColor">T'+i+' &#183; '+esc(f.type||"")+'</text><text x="46" y="'+(y+36)+'" font-size="12" fill="currentColor" opacity=".7">Filament '+(i+1)+'</text>'});
  MHEADS.forEach(function(x){var y=x.i*(RH+GAP)+2,off=x.loaded===false;
    cards+='<rect x="'+X2+'" y="'+y+'" width="'+CW+'" height="'+RH+'" rx="9" fill="var(--hd)" stroke="var(--orca-border)"/><text x="'+(X2+16)+'" y="'+(y+RH/2+5)+'" font-size="14" font-weight="700" fill="currentColor">T'+x.i+' &#183; '+esc(off?"empty":(x.mat||""))+'</text><circle cx="'+(X2+CW-24)+'" cy="'+(y+RH/2)+'" r="11" fill="'+(off?'none':esc(x.colour||"#808080"))+'" stroke="rgba(128,128,128,.7)"/>'});
  $("mRows").innerHTML='<div class="mcol"><span>IN THE FILE</span><span>ON THE TOOLHEADS</span></div>'+svg+cards+lines+'</svg>';
  $("mWarn").innerHTML=warn.map(function(w){return'<div class="mw"><b>&#9888;</b><span>'+esc(w)+'</span></div>'}).join("")}
function mapUpdate(s,sl,vars){
  MHEADS=[];[0,1,2,3].forEach(function(i){var f=s["filament_switch_sensor filament"+i];if(!f)return;var k=sl[i]||{};MHEADS.push({i:i,colour:k.colour||"",mat:k.material||"",loaded:f.filament_detected})});
  for(var t=0;t<4;t++){var v=Number(vars["box_modify_t"+t+"_backup"]);if(!(v>=0&&v<4))v=Number(vars["box_modify_t"+t]);MAP[t]=(v>=0&&v<4)?v:t}
  renderMap()}
function act(p){p.type="action";$("res").className="muted";$("res").textContent="sending...";orca.postMessage(p)}
function mv(a,d){act({kind:"move",axis:a,dist:d*STEP})}
function ex(d){act({kind:"extrude",dist:d})}
function swatch(c){return c?'<span class="sw" style="background:'+esc(c)+'"></span>':''}
function render(m){
  var s=m.status||{},ps=s.print_stats||{},ds=s.display_status||{},vs=s.virtual_sdcard||{};
  HASTMT1=!!m.hasTmt1;if(m.lightOn!==undefined)LIGHTON=m.lightOn;if(m.helperOk!==undefined)HELPEROK=!!m.helperOk;
  BASE=m.base;CAMS=m.cams||[];if(camOn&&!$("cam").getAttribute("src"))setCam();
  if(FIRST){FIRST=false;loadFiles();loadHist()}
  $("err").textContent="";$("conn").textContent="connected to "+m.base.replace("http://","");$("navTxt").textContent=m.base.replace("http://","").replace(":7125","");$("navDot").className="dot on";
  var st=ps.state||"-";if(LASTST&&LASTST!==st&&LASTST==="printing"&&(st==="complete"||st==="error"||st==="cancelled"||st==="paused"))toast("Print "+st+(ps.filename?": "+ps.filename:""));LASTST=st;STATE=st;var e=$("state");e.textContent=st;e.className="state s-"+st;var e2=$("st2");e2.textContent=st;e2.className="big "+(st==="printing"?"ok":(st==="error"?"err":""));
  $("bPause").disabled=st!=="printing";$("bResume").disabled=st!=="paused";$("bCancel").disabled=!(st==="printing"||st==="paused");mvState();
  $("file").textContent=ps.filename||"";
  var wh0=s.webhooks||{},kb=$("klip");if(wh0.state&&wh0.state!=="ready"){kb.style.display="block";kb.innerHTML="<b class='err'>Klipper is "+esc(wh0.state)+"</b><div class='muted'>"+esc(wh0.state_message||"")+"</div>"}else kb.style.display="none";
  var p=(ds.progress!==undefined&&ds.progress!==null?ds.progress:vs.progress)||0;
  $("bar").style.width=Math.round(p*100)+"%";var rp=Math.round(p*100);$("ring").style.background="conic-gradient(var(--g) 0 "+rp+"%,rgba(128,128,128,.25) 0)";$("ringTxt").textContent=rp+"%";$("pct").textContent=Math.round(p*100)+" %";
  var inf=ps.info||{};$("layer").textContent=inf.total_layer?("layer "+(inf.current_layer||0)+" / "+inf.total_layer):"";
  var left=(st==="printing"&&p>0.02)?Math.max(0,ps.print_duration/p-ps.print_duration):null;
  $("times").textContent="printing "+tt(ps.print_duration)+(left!==null?" · about "+tt(left)+" left":"")+" · total "+tt(ps.total_duration);
  SLOTS=m.slots||[];ZRV=!!s["gcode_macro _ZR_SET_TOOL_INFO"];var cur=(s.toolhead||{}).extruder||"extruder";var h="";var names=["extruder","extruder1","extruder2","extruder3"];var sl=m.slots||[];
  var hasSensors=!!s["filament_switch_sensor filament0"];
  if(hasSensors){
  for(var i=0;i<4;i++){var x=s[names[i]];if(!x)continue;
    var f=s["filament_switch_sensor filament"+i],d=s["filament_switch_sensor docked"+i],k=sl[i]||{};
    var present=f&&f.filament_detected,col=k.colour||"",act=cur===names[i];
    var inner=(col||k.material)?((col?'<span style="position:absolute;inset:0;display:flex">'+spool(col,"100%")+'</span>':'')+(col?'':'<span>'+esc(k.material||"set")+'</span>')+(!present?'<small>no filament</small>':'')+'<span class="pen">&#9998;</span>'):('<span>'+(present?"Set colour":"Empty")+'</span><span class="pen">&#9998;</span>');
    var st2=(present||!col?'':'opacity:.55;');
    h+='<div class="slot'+(act?' act':'')+((col||k.material)?'':' empty')+'" onclick="editSpool('+i+')"><div class="spl'+(col?' sp':'')+'" style="'+st2+'">'+inner+'</div>'
     +'<div class="tl">T'+(i+1)+(col&&k.material?' '+esc(k.material):'')+(act?' (active)':'')+'</div><div class="tt">'+t(x.temperature)+' / '+(x.target>0?t(x.target):"off")+'</div>'
     +'<div class="tt"><span class="dot '+(present?"on":"red")+'"></span>'+(present?"filament":"empty")+' <span class="dot '+(d&&d.filament_detected?"on":"off")+'" style="margin-left:6px"></span>'+(d&&d.filament_detected?"docked":"out")+'</div>'+tgl(s["filament_switch_sensor path_"+i])+'<svg class="spark" id="hsp'+i+'"></svg></div>';}
  }else if(s.ace&&s.ace.lanes){
    s.ace.lanes.forEach(function(l){var has=!!(l.material||l.color),col=l.color||"";
      h+='<div class="slot'+(has?'':' empty')+'"><div class="spl'+(col?' sp':'')+'">'+(col?'<span style="position:absolute;inset:0;display:flex">'+spool(col,"100%")+'</span>':'<span>'+(has?esc(l.material||l.filament_name||""):"Empty")+'</span>')+'</div><div class="tl">ACE '+(parseInt(l.lane)+1)+(col&&(l.material||l.filament_name)?' '+esc(l.material||l.filament_name):'')+'</div></div>';});
    var x0=s.extruder;if(x0)h+='<div class="slot act"><div class="spl" style="height:84px"><span>'+t(x0.temperature)+'</span><small>nozzle</small></div><div class="tl">Nozzle</div><div class="tt">target '+(x0.target>0?t(x0.target):"off")+'</div></div>';
  }
  $("heads").innerHTML=h;renderAce(s);renderFil(s,sl);
  var ax=s[cur];$("nz").textContent=ax?t(ax.temperature):"-";$("nzt").textContent=ax?("target "+(ax.target>0?t(ax.target):"off")+" · power "+pc(ax.power)):"";
  for(var q=0;q<4;q++)setSpark("hsp"+q,"h"+q,"#2e9d4f");setSpark("bedsp","bed","#d9a21b");setSpark("chamsp","cham","#2b7bd8");
  var th=s.toolhead||{},gm=s.gcode_move||{},po=th.position||[];
  $("pos").textContent=po.length>=3?(po[0].toFixed(1)+" / "+po[1].toFixed(1)+" / "+po[2].toFixed(2)):"-";
  var hx=(th.homed_axes||"").toUpperCase();$("homed").innerHTML=["X","Y","Z"].map(function(a){return '<span class="badge '+(hx.indexOf(a)>=0?"b-ok":"b-no")+'">'+a+'</span>'}).join("");
  $("idle").textContent=(s.idle_timeout||{}).state||"-";$("ext").textContent=pc(gm.extrude_factor);
  var b=s.heater_bed||{},c=s["heater_generic chamber"]||{};
  pushT("bed",b.temperature);pushT("cham",c.temperature);names.forEach(function(n,i){pushT("h"+i,(s[n]||{}).temperature)});
  $("bed").textContent=t(b.temperature);$("bedt").textContent="target "+(b.target>0?t(b.target):"off")+" · power "+pc(b.power);
  $("cham").textContent=c.temperature===undefined?"-":t(c.temperature);$("chamt").textContent=c.temperature===undefined?"no chamber sensor":"target "+(c.target>0?t(c.target):"off");
  $("f0").textContent=pc((s.fan||{}).speed);$("f1").textContent=pc((s["fan_generic auxiliary_fan"]||{}).speed);$("f2").textContent=pc((s["fan_generic air_fan"]||{}).speed);
  $("spd").textContent=pc((s.gcode_move||{}).speed_factor);LS=s;LNAMES=names;syncCtl(s);syncHeat(s)
  var ms=m.messages||[];var html="";var wh=s.webhooks||{};
  if(wh.state&&wh.state!=="ready")html+='<div class="msg err">Klipper: '+esc(wh.state)+' - '+esc(wh.state_message)+'</div>';
  if(ps.message)html+='<div class="msg err">print: '+esc(ps.message)+'</div>';
  var groups=[];ms.slice().reverse().forEach(function(x){var g=groups[groups.length-1];if(g&&g.text===x.text){g.n++}else groups.push({text:x.text,t:x.t,n:1})});
  groups.forEach(function(g){html+='<div class="msg">'+new Date(g.t*1000).toLocaleTimeString()+'  '+(g.n>1?'<span class="cnt">x'+g.n+'</span> ':'')+esc(g.text)+'</div>'});
  $("msgs").innerHTML=html||"no errors or warnings in the recent console";
  $("upd").textContent="updated "+new Date(m.time*1000).toLocaleTimeString();}
orca.onMessage(function(m){
  if(m.type==="state")render(m);
  if(m.type==="defaults"){(m.hosts||[]).forEach(addOpt);if(m.host)addOpt(m.host);if(!$("addr").value.trim()){if(m.last){$("addr").value=m.last;addOpt(m.last);syncPreset(m.last);connect()}else if(m.host){$("addr").value=m.host;syncPreset(m.host)}}}
  if(m.type==="orcafils"){FILS=m.fils||[];FILERR=m.err||"";if(LS&&LS.print_stats)renderFil(LS,SLOTS)}
  if(m.type==="approve"){$("err").textContent="New printer "+m.host+": Orca approves a printer address only while the plugin loads. It is saved now. Close Orca and open it again, approve the permission dialog for this printer, then press Connect. You only do this once per new printer.";$("conn").textContent="needs approval";return}
  if(m.type==="error"){$("err").textContent="Cannot read the printer: "+m.text;$("conn").textContent="not connected"}
  if(m.type==="prompt"){var pm=$("pmodal");if(m.close||!m.prompt){pm.style.display="none"}else{showPrompt(m.prompt)}}
  if(m.type==="result"){$("res").className=m.ok?"ok":"err";$("res").textContent=m.text;if(m.lightOn!==undefined){LIGHTON=m.lightOn;var bl=$("btnLight");if(bl)bl.classList.toggle("on",LIGHTON==="on")}}
  if(m.type==="install_helper_result"){onInstallHelperResult(m.ok,m.detail)}
  if(m.type==="probe"){$("probetxt").value=m.text||""}
  if(m.type==="timelapse"){TLV=m.vids||[];TLERR=m.error||"";renderTL()}
  if(m.type==="history"){HJOBS=m.jobs||[];HTOT=m.totals||null;HERR=m.error||"";renderHist()}
  if(m.type==="files"){FROWS=m.files;FROWS.sort(function(a,b){return b.modified-a.modified});renderFiles()}
  if(m.type==="filemeta"){for(var fk in m.meta)FMETA[fk]=m.meta[fk];renderFiles();if(HJOBS.length)renderHist()}
});
try{(JSON.parse(localStorage.getItem("wmdev_addrs")||"[]")).forEach(addOpt)}catch(e){}
try{var a=localStorage.getItem("wmdev_addr");if(a){$("addr").value=a;addOpt(a);syncPreset(a);connect()}else{orca.postMessage({type:"defaults"})}}catch(e){}
(function(){var n=0;function ask(){try{if(!$("addr").value.trim()&&n<12){n++;orca.postMessage({type:"defaults"});setTimeout(ask,1500)}}catch(e){setTimeout(ask,1500)}}setTimeout(ask,800)})();
</script></body></html>"""


MAPSTATE = {"write": False, "map": None, "base": ""}     # set by the Device tab (UI thread), read by the slicing step (worker thread)


def parse_config_list(value):
    """Orca vector option ('#AA0000;#00FF00' or '"PLA";"PETG"') -> list of strings."""
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    items, cur, in_q, esc, quoted = [], [], False, False, False
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\" and in_q:
            esc = True
        elif ch == '"':
            in_q = not in_q
            quoted = True
        elif ch == ";" and not in_q:
            items.append("".join(cur).strip() if not quoted else "".join(cur))
            cur, quoted = [], False
        else:
            cur.append(ch)
    items.append("".join(cur).strip() if not quoted else "".join(cur))
    return items


def project_filaments(colours, types, names):
    count = max(len(colours), len(types), len(names))

    def pick(lst, i):
        if i < len(lst):
            return lst[i]
        return lst[0] if len(lst) == 1 else ""

    return [{"index": i, "colour": pick(colours, i), "type": pick(types, i), "name": pick(names, i)} for i in range(count)]


def read_orca_filaments():
    """Filaments of the open Orca project (read only). Returns (list, error text)."""
    try:
        bundle = orca.host.preset_bundle()
        val = lambda k: bundle.full_config_value(k)  # noqa: E731
        return project_filaments(parse_config_list(val("filament_colour")), parse_config_list(val("filament_type")),
                                 parse_config_list(val("filament_settings_id"))), ""
    except Exception as exc:  # noqa: BLE001
        return [], repr(exc)


def probe_orca_api():
    """Read-only look at what this Orca offers to plugins (names and help text only, nothing is called)."""
    out = []

    def names(label, obj):
        try:
            out.append(label + ": " + ", ".join(n for n in dir(obj) if not n.startswith("_")))
        except Exception as exc:  # noqa: BLE001
            out.append(label + ": error " + repr(exc)[:100])

    names("orca", orca)
    host = getattr(orca, "host", None)
    names("orca.host", host)
    seen = []
    for path in ("preset", "preset.filament", "preset.bundles", "preset.printer", "preset.process", "ui", "plater"):
        obj = host
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except Exception as exc:  # noqa: BLE001
            out.append("orca.host." + path + ": not there (" + repr(exc)[:60] + ")")
            continue
        names("orca.host." + path, obj)
        seen.append((path, obj))
    for path, obj in seen:
        for n in ("begin_update", "set_slot_color", "reload_local", "reload_presets", "slots", "list", "get", "set"):
            try:
                f = getattr(obj, n, None)
                if f is not None and callable(f):
                    out.append("help orca.host.%s.%s: %s" % (path, n, " ".join(str(getattr(f, "__doc__", "") or "").split())[:300]))
            except Exception:  # noqa: BLE001
                pass
    try:
        out.append("orca version: " + str(getattr(orca, "__version__", "") or getattr(host, "version", "") or "?"))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out)


def clean_map(value):
    """[a,b,c,d] of toolhead numbers 0-3, else None."""
    if not isinstance(value, (list, tuple)) or len(value) != HEADS:
        return None
    try:
        out = [int(v) for v in value]
    except (TypeError, ValueError):
        return None
    return out if all(0 <= v < HEADS for v in out) else None


def map_gcode_line(mapping, out_name):
    base = os.path.basename(str(out_name or "")).replace('"', "").replace("'", "").replace("\\", "")
    line = "WM_SET_TOOL_MAP MAP=" + ",".join(f"{t}:{v}" for t, v in enumerate(mapping))
    if base:
        line += f' FILE="{base}"'
    return line + " KEEP_EXISTING=1\n"


def map_native_lines(mapping):
    """Same map for a factory printer that has no WM_SET_TOOL_MAP macro: plain Klipper SAVE_VARIABLE lines (box_modify_tN
    is what the factory T0-T3 macros read)."""
    out = []
    for t, v in enumerate(mapping):
        out.append(f"SAVE_VARIABLE VARIABLE=box_modify_t{t} VALUE={v}\n")
        out.append(f"SAVE_VARIABLE VARIABLE=box_modify_t{t}_backup VALUE={v}\n")
    return "".join(out)


def printer_has_macro(base, name):
    """True when the printer has this gcode macro. When it cannot be checked, assume yes (the Pi default)."""
    try:
        objs = http_json(base + "/printer/objects/list", timeout=6)["result"]["objects"]
    except Exception:  # noqa: BLE001
        return True
    return ("gcode_macro " + name).lower() in [str(o).lower() for o in objs]


def insert_map_line(lines, new_line):
    """Insert after the leading comment header, before the first real command. Does nothing when a map is already there."""
    if any(l.startswith(("WM_SET_TOOL_MAP", "ACE_SET_TOOL_MAP", "; WM Device: tool map", "SAVE_VARIABLE VARIABLE=box_modify_t"))
           for l in lines[:400]):
        return lines, False
    idx = len(lines)
    for i, l in enumerate(lines):
        st = l.strip()
        if st and not st.startswith(";"):
            idx = i
            break
    return lines[:idx] + ["; WM Device: tool map from the Device tab\n", new_line] + lines[idx:], True


# ---- Send / Upload popup (printer agent) -------------------------------------------------------------------
POPUP = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
:root{--g:#00AE42}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;background:#1d2023;color:#eceff1;font-size:15px}
#top{padding:14px 20px;border-bottom:1px solid #353a40}
#top h1{margin:0;font-size:19px}
#sub{color:#9aa4ad;font-size:13px;margin-top:4px}
#tools{display:flex;gap:10px;padding:10px 20px;align-items:center;position:relative}
#tools button.lnk{background:none;border:0;color:#5aa9ff;font-weight:700;font-size:13px;cursor:pointer;padding:6px 4px}
#sv{position:absolute;top:44px;left:20px;background:#2a2e33;border:1px solid #4a5058;border-radius:8px;padding:8px;display:none;z-index:5;min-width:260px}
#sv .it{display:flex;justify-content:space-between;gap:8px;padding:5px 4px;cursor:pointer}
#sv input{width:120px;background:#1d2023;color:#eee;border:1px solid #4a5058;border-radius:6px;padding:4px}
#rows{padding:0 20px}
.r{display:flex;align-items:center;gap:8px;background:#272b30;border-left:4px solid #e0a030;margin:8px 0;padding:9px 10px;border-radius:6px}
.r.ok{border-left-color:#35c46a}
.dot{width:20px;height:20px;border-radius:50%;border:1px solid #777;flex:none}
.n{width:112px;flex:none;font-weight:700}.n small{display:block;color:#9aa4ad;font-weight:400;font-size:12px}
.h{flex:1;min-width:0;text-align:center;padding:8px 2px;border-radius:6px;background:#30353b;border:2px solid transparent;color:#eceff1;font-size:13px;cursor:pointer;white-space:nowrap}
.h.sel{background:#2b4a70;border-color:#4a90e2}.h.off{opacity:.4;cursor:default}
.h i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:middle;border:1px solid #777}
.st{width:66px;flex:none;font-weight:700;font-size:13px;text-align:right}
.mw.bad{background:#5a1f22;color:#ff9a9a;border:1px solid #c0392b;font-weight:700}
.mw{background:#4a3b1c;color:#f0c060;padding:9px 12px;border-radius:6px;font-size:13px;margin:6px 20px}
#desc{margin:0 20px 8px;padding:10px 12px;border:1px solid #353a40;border-radius:10px;background:#23272c;line-height:1.65;font-size:13px;color:#9aa4ad}#desc b{color:#e6e9ec;font-weight:600}#desc b.g{color:#5ec27a}
#foot{display:flex;gap:10px;padding:14px 20px;border-top:1px solid #353a40;align-items:center;margin-top:8px}
#foot span{flex:1;color:#9aa4ad;font-size:12px}
button.b{padding:11px 20px;border:0;border-radius:7px;font-size:15px;font-weight:700;color:#fff;cursor:pointer;background:#3b4046}
button.up{background:#2a6fb5}button.pr{background:#1fa150}
</style></head><body>
<div id="top"><h1>Send to printer: choose toolheads</h1><div id="sub"></div></div>
<div id="tools"><button class="lnk" onclick="auto()">AUTO-MATCH</button><button class="lnk" onclick="done('sync')">SYNC PRINTER FILAMENT</button>
<button class="lnk" onclick="svt()">SAVED MAPPINGS &#9662;</button><div id="sv"></div></div>
<div id="rows"></div><div id="warn"></div>
<div id="desc"><div><b>Cancel</b> sends nothing.</div><div><b>Upload</b> sends the file with your choice written into it. It does not print.</div><div><b class="g">Upload and Print</b> sends the file with your choice and starts the print.</div></div>
<div id="foot"><span id="note"></span><button class="b" onclick="done('cancel')">Cancel</button><button class="b up" onclick="done('upload')">Upload</button><button class="b pr" onclick="done('print')">Upload and Print</button></div>
<script>
var D=__DATA__,PALN=__PAL__,MAP=D.map.slice(0,4);
function $(i){return document.getElementById(i)}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function hx(c){var m=/^#?([0-9a-f]{6})/i.exec(c||"");return m?[parseInt(m[1].substr(0,2),16),parseInt(m[1].substr(2,2),16),parseInt(m[1].substr(4,2),16)]:null}
function cdist(a,b){var x=hx(a),y=hx(b);if(!x||!y)return 400;var rm=(x[0]+y[0])/2,dr=x[0]-y[0],dg=x[1]-y[1],db=x[2]-y[2];return Math.sqrt((2+rm/256)*dr*dr+4*dg*dg+(2+(255-rm)/256)*db*db)}
function cname(c){var b="",bd=1e9;PALN.forEach(function(p){var d=cdist(c,p[1]);if(d<bd){bd=d;b=p[0]}});return b}
function sameMat(a,b){a=String(a||"").toUpperCase().trim();b=String(b||"").toUpperCase().trim();return !!a&&!!b&&(a===b||b.indexOf(a)===0||a.indexOf(b)===0)}
var H=D.heads,ROWS=D.fils.length?D.fils.slice(0,4):H.map(function(h,i){return{index:i,colour:h.colour,type:h.mat}});
function calc(){var res=ROWS.map(function(){return-1}),used={};
  ROWS.forEach(function(f,i){var v=D.cur[i];if(!(v>=0&&v<4))v=i;var h=H[v];if(h&&h.loaded!==false&&!used[v]&&sameMat(f.type,h.mat)&&cdist(f.colour,h.colour)<=300){res[i]=v;used[v]=1}});
  var pairs=[];ROWS.forEach(function(f,i){if(res[i]>=0)return;H.forEach(function(h){if(h.loaded===false)return;pairs.push({i:i,h:h.i,d:cdist(f.colour,h.colour)+(sameMat(f.type,h.mat)?0:1000)})})});
  pairs.sort(function(a,b){return a.d-b.d});pairs.forEach(function(p){if(res[p.i]<0&&!used[p.h]){res[p.i]=p.h;used[p.h]=1}});
  ROWS.forEach(function(f,i){if(res[i]>=0)return;var b=-1,bd=1e9;H.forEach(function(h){if(h.loaded===false)return;var d=cdist(f.colour,h.colour);if(d<bd){bd=d;b=h.i}});res[i]=b>=0?b:i});return res}
function auto(){var r=calc();ROWS.forEach(function(f,i){MAP[i]=r[i]});draw()}
function pick(i,h){if(H[h]&&H[h].loaded===false)return;var old=MAP[i];for(var j=0;j<ROWS.length;j++){if(j!==i&&MAP[j]===h){MAP[j]=old;break}}MAP[i]=h;CONF=false;draw()}
function wedge(a0,a1,r1,r2){function q(r,a){return (r*Math.cos(a)).toFixed(2)+" "+(r*Math.sin(a)).toFixed(2)}return "M"+q(r2,a0)+" A"+r2+" "+r2+" 0 0 1 "+q(r2,a1)+" L"+q(r1,a1)+" A"+r1+" "+r1+" 0 0 0 "+q(r1,a0)+" Z"}
function spool(c,z){c=esc(c||"#808080");z=z||20;var k="rgba(0,0,0,.6)",s='<svg viewBox="-50 -50 100 100" width="'+z+'" height="'+z+'" style="flex:none;vertical-align:middle"><circle r="47" fill="'+c+'" stroke="'+k+'" stroke-width="4"/>';for(var i=0;i<6;i++){var m=i*Math.PI/3-Math.PI/2;s+='<path d="'+wedge(m-.36,m+.36,20,38)+'" fill="#1b1d20" stroke="'+k+'" stroke-width="3" stroke-linejoin="round"/>'}return s+'<circle r="13" fill="'+c+'" stroke="'+k+'" stroke-width="4"/><circle r="6" fill="#1b1d20" stroke="'+k+'" stroke-width="2"/></svg>'}
function draw(){var users={},warn=[],h="";ROWS.forEach(function(f,i){users[MAP[i]]=(users[MAP[i]]||0)+1});
  ROWS.forEach(function(f,i){var hd=H[MAP[i]]||{},ok=hd.loaded!==false&&sameMat(f.type,hd.mat)&&cdist(f.colour,hd.colour)<=300;
    if(!ok)warn.push(hd.loaded===false?"Toolhead "+(MAP[i]+1)+" is empty: load filament before printing filament "+(i+1)+".":"Filament "+(i+1)+" ("+cname(f.colour)+" "+(f.type||"")+") does not match toolhead "+(MAP[i]+1)+" ("+(hd.colour?cname(hd.colour):"no colour")+" "+(hd.mat||"")+"). Pick another or send anyway.");
    h+='<div class="r '+(ok?'ok':'')+'">'+spool(f.colour,28)+'<div class="n">Filament '+(i+1)+'<small>'+esc(f.type||"")+'</small></div>'
    +H.map(function(x){var off=x.loaded===false;return'<button class="h'+(MAP[i]===x.i?' sel':'')+(off?' off':'')+'" onclick="pick('+i+','+x.i+')"><i style="background:'+(off?'transparent':esc(x.colour||"#808080"))+'"></i>T'+x.i+' '+(off?'empty':esc(x.mat||""))+'</button>'}).join("")
    +'<span class="st" style="color:'+(ok?'#35c46a':'#e0a030')+'">'+(ok?'MATCH':'CHECK')+'</span></div>'});
  Object.keys(users).forEach(function(k){if(users[k]>1)warn.push("Toolhead "+(Number(k)+1)+" will print "+users[k]+" file colours with the same filament.")});
  $("rows").innerHTML=h;$("warn").innerHTML=warn.map(function(w){return'<div class="mw">&#9888; '+esc(w)+'</div>'}).join("")}
var SAVED={};try{SAVED=JSON.parse(localStorage.getItem("wmdev_popup_maps")||"{}")||{}}catch(e){}
function store(){try{localStorage.setItem("wmdev_popup_maps",JSON.stringify(SAVED))}catch(e){}}
function svt(){var e=$("sv");if(e.style.display==="block"){e.style.display="none";return}var h="";Object.keys(SAVED).forEach(function(n,i){h+='<div class="it"><span onclick="ld('+i+')">'+esc(n)+' <small>'+SAVED[n].map(function(v,k){return"T"+k+"&rarr;"+v}).join(" ")+'</small></span><span onclick="dl('+i+')">x</span></div>'});
  if(!h)h='<div class="it">none saved yet</div>';h+='<div class="it"><input id="nm" placeholder="name"><button class="lnk" onclick="sv()">Save current</button></div>';e.innerHTML=h;e.style.display="block"}
function sv(){var n=($("nm").value||"").replace(/[<>"']/g,"").trim();if(!n)return;SAVED[n]=MAP.slice(0,4);store();$("sv").style.display="none"}
function ld(i){var v=SAVED[Object.keys(SAVED)[i]];if(v&&v.length===4){MAP=v.slice();draw()}$("sv").style.display="none"}
function dl(i){delete SAVED[Object.keys(SAVED)[i]];store();$("sv").style.display="none"}
var CONF=false;
function badMat(){var o=[];ROWS.forEach(function(f,i){var hd=H[MAP[i]]||{};if(hd.loaded!==false&&f.type&&hd.mat&&!sameMat(f.type,hd.mat))o.push("Filament "+(i+1)+" is "+f.type+" but toolhead "+(MAP[i]+1)+" has "+hd.mat)});return o}
function done(a){if((a==="upload"||a==="print")&&!CONF){var b=badMat();if(b.length){CONF=true;$("warn").innerHTML='<div class="mw bad">&#9888; Different material: '+esc(b.join("; "))+'. Press the button again to continue anyway, or pick another toolhead.</div>'+$("warn").innerHTML;return}}
  orca.submit({action:a,map:MAP.slice(0,4)})}
$("sub").textContent=D.file+" · "+ROWS.length+" filament"+(ROWS.length===1?"":"s")+" · printer "+D.host;
$("note").textContent=D.note||"";
(function(){var m=D.mem;if(m&&m.length===4&&ROWS.length&&m.every(function(v){return v>=0&&v<4&&!(H[v]&&H[v].loaded===false)})){MAP=m.slice();draw();$("note").textContent="Map from the last print of this project";}else auto()})();
</script></body></html>"""


def fetch_heads(base):
    """Live toolheads of the printer (colour, material, loaded) and the saved map, read only. Returns (heads, current map, error)."""
    try:
        slots = parse_slots(http_text(base + "/server/files/config/tmt1.ini"))
    except Exception:  # noqa: BLE001
        slots = [{"colour": "", "material": ""} for _ in range(HEADS)]
    q = "&".join(urllib.parse.quote(o, safe="") for o in
                 ["save_variables"] + ["filament_switch_sensor filament%d" % i for i in range(HEADS)])
    status = http_json(base + "/printer/objects/query?" + q, timeout=6)["result"]["status"]
    merged = merge_saved(slots, status)
    vars_ = (status.get("save_variables") or {}).get("variables") or {}
    heads, cur = [], []
    for i in range(HEADS):
        sensor = status.get("filament_switch_sensor filament%d" % i) or {}
        heads.append({"i": i, "colour": merged[i].get("colour", ""), "mat": merged[i].get("material", ""),
                      "loaded": sensor.get("filament_detected") if sensor else True})
        try:
            cur.append(int(vars_.get("box_modify_t%d" % i, i)))
        except (TypeError, ValueError):
            cur.append(i)
    return heads, cur, ""


def popup_html(fils, heads, cur, filename, host, note="", remembered=None):
    data = {"mem": remembered, "fils": fils[:HEADS], "heads": heads, "cur": cur, "map": cur, "file": os.path.basename(filename), "host": host, "note": note}
    pal = [[n, "#%06X" % v] for n, v in TOUCH_PALETTE]
    safe = lambda o: json.dumps(o).replace("</", "<\\/")  # noqa: E731
    return POPUP.replace("__DATA__", safe(data)).replace("__PAL__", safe(pal))


def ui_dialog(ui, html, title, width, height, timeout=900):
    """Modal HTML window that waits for the page's submit and returns its payload (None when closed). This Orca has no
    show_dialog, only create_window(html, title, w, h, on_message, on_close, style, on_submit)."""
    if hasattr(ui, "show_dialog"):
        return ui.show_dialog(html, title, width, height)
    ev, box = threading.Event(), {}

    def on_submit(data):
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except Exception:  # noqa: BLE001
                pass
        box["r"] = data
        ev.set()

    def on_close(*_a):
        ev.set()

    try:
        _trace("thread=%s main=%s ui_names=%s" % (threading.current_thread().name, threading.current_thread() is threading.main_thread(),
               [n for n in dir(ui) if n.isupper() or "window" in n.lower() or "dialog" in n.lower()]))
    except Exception as exc:  # noqa: BLE001
        _trace("thread info error " + repr(exc))
    _trace("create_window: calling (html %d bytes)" % len(html))
    # Modeless: a modal window opened behind Orca's main window blocks all clicks and Orca looks frozen. Modeless keeps Orca usable.
    style = getattr(ui, "WINDOW_MODELESS", None)
    if style is None:
        style = ui.WINDOW_MODAL
    win = ui.create_window(html, title, int(width), int(height), None, on_close, style, on_submit)
    _trace("create_window: returned, waiting for the user")
    ev.wait(timeout)
    _trace("create_window: wait finished")
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    return box.get("r")



def _swap_desc(h, inner):
    """Replace the text of the description box above the buttons of a mapping window."""
    a = h.index('<div id="desc">') + len('<div id="desc">')
    b = h.index('</div>\n<div id="foot">')
    return h[:a] + inner + h[b:]


def slice_popup_html(fils, heads, cur, filename, host, note="", remembered=None):
    h = popup_html(fils, heads, cur, filename, host, note, remembered)
    h = h.replace("Send to printer: choose toolheads", "Sliced: choose toolheads for this print")
    h = h.replace('<button class="b pr" onclick="done(\'print\')">Upload and Print</button>', "")
    h = h.replace(">Upload</button>", ">Apply mapping</button>")
    h = h.replace("<button class=\"b up\"", "<button class=\"b\" onclick=\"done('skip')\">Skip mapping</button><button class=\"b up\"", 1)
    h = _swap_desc(h, '<div><b class="g">Apply mapping</b> writes this choice into the G-code and remembers it for this project.</div><div><b>Skip mapping</b> leaves the G-code as it is. Nothing is written or remembered.</div><div><b>Cancel</b> makes the file empty so it cannot be printed. Slice again.</div>')
    return h


def file_popup_html(fils, heads, cur, filename, host, note="", remembered=None):
    h = popup_html(fils, heads, cur, filename, host, note, remembered)
    h = h.replace("Send to printer: choose toolheads", "Print: choose toolheads for this file")
    h = h.replace('<button class="b up" onclick="done(\'upload\')">Upload</button>', "")
    h = h.replace(">Upload and Print</button>", ">Apply and print</button>")
    h = h.replace('<button class="b" onclick="done(\'cancel\')">Cancel</button>', '<button class="b" onclick="done(\'cancel\')">Cancel</button><button class="b" onclick="done(\'skip\')">Print without map</button>', 1)
    h = _swap_desc(h, '<div><b class="g">Apply and print</b> sets this choice on the printer, remembers it for this file and starts the print.</div><div><b>Print without map</b> starts the print and leaves the printer\'s tool map as it is.</div><div><b>Cancel</b> sends nothing.</div>')
    return h


def apply_tool_map(path, mapping, name, native=False):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    lines, done = insert_map_line(lines, map_native_lines(mapping) if native else map_gcode_line(mapping, name))
    if done:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return done


def ask_tool_map(path, name, ui=None):
    """Popup shown when slicing finishes. Returns (mapping or None, message)."""
    _trace("ask_tool_map start %s thread=%s main=%s" % (name, threading.current_thread().name, threading.current_thread() is threading.main_thread()))
    if threading.current_thread() is threading.main_thread():
        _trace("main thread: popup skipped (waiting here would freeze Orca)")
        return None, "popup skipped: the export step runs on Orca's main thread"
    ui = ui or orca.host.ui
    base = MAPSTATE.get("base") or ""
    if not base:
        return None, "no printer connected in the WM Device tab"
    note = ""
    while True:
        try:
            _trace("fetch_heads")
            heads, cur, _ = fetch_heads(base)
        except Exception as exc:  # noqa: BLE001
            _trace("fetch_heads error " + repr(exc))
            return None, "printer not reachable: " + repr(exc)
        _trace("read_orca_filaments")
        fils, ferr = read_orca_filaments()
        _trace("filaments done " + str(ferr)[:80])
        if ferr:
            note = "Orca filaments not available: showing the printer's own tools"
        key = mem_key(name, len(fils) or HEADS)
        _trace("mem_load " + key)
        mem = mem_load(key)
        _trace("building popup html")
        page = slice_popup_html(fils, heads, cur, name, base.replace("http://", "").replace(":7125", ""), note, mem)
        _trace("popup html built")
        res = ui_dialog(ui, page, "WM Device: tool mapping", 860, 720)
        _trace("dialog returned " + str((res or {}).get("action")))
        act = (res or {}).get("action")
        if act == "sync":
            note = "Synced from the printer"
            continue
        if act == "skip":
            return None, "skipped"
        if act not in ("upload", "print"):
            return None, "cancelled"
        got = clean_map((res or {}).get("map"))
        if got:
            mem_save(key, got)
        return got, "ok"


class WmAbort(RuntimeError):
    pass


CANCEL_GCODE = (
    "; WM Tool Map: CANCELLED in the mapping window - this file is intentionally empty, do not print.\n"
    "; Slice again and press Apply mapping or Skip mapping to get the real G-code.\n"
    "M117 CANCELLED in Orca - nothing to print\n"
    "RESPOND TYPE=error MSG=\"WM Tool Map: print cancelled in Orca. This file has no moves.\"\n")


def neutralize_gcode(path):
    """Orca ignores a failed slicing step and still sends the file, so on Cancel the G-code is replaced by a file
    with no heating and no moves. A cancelled job then cannot run on the printer."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(CANCEL_GCODE)


def abort_result(msg):
    """Ask Orca to stop the export/upload. Tries the failure results Orca may offer, then raises so the step counts as failed."""
    for name in ("failure", "fail", "error", "fatal_error", "fatal", "cancel", "cancelled", "abort", "skipped", "failed"):
        fn = getattr(orca.ExecutionResult, name, None)
        if callable(fn):
            try:
                return fn(msg)
            except Exception:  # noqa: BLE001
                continue
    raise WmAbort(msg)


_SlicingBase = getattr(getattr(orca, "slicing", None), "SlicingPipelineCapabilityBase", None)
if _SlicingBase is not None:
    class WmToolMapStep(_SlicingBase):
        """Optional G-code step. When a printer is connected in the Device tab it opens the tool mapping popup after slicing
        and writes the pick into the file; otherwise it writes the map saved in the Device tab (if switched on)."""

        def get_name(self):
            return "WM Tool Map"

        def execute(self, ctx):
            path = getattr(ctx, "gcode_path", "") or ""
            _trace("execute step=" + str(getattr(ctx, "step", "")) + " base=" + str(MAPSTATE.get("base")))
            try:
                if ctx.step != orca.slicing.Step.psGCodePostProcess or not path:
                    return orca.ExecutionResult.success()
                mapping, why = None, ""
                if not MAPSTATE.get("base"):
                    cand = orca_print_host()
                    if cand and PERM["ok"] and _host_of(cand) not in PERM["hosts"]:
                        remember_host(cand, auto=True)  # not approved at load: no network now (it would prompt once per URL)
                        cand = ""
                    MAPSTATE["base"] = cand
                if MAPSTATE.get("popup", True) and MAPSTATE.get("base"):
                    try:
                        mapping, why = ask_tool_map(path, ctx.output_name)
                    except Exception as exc:  # noqa: BLE001
                        mapping, why = None, "popup failed: " + repr(exc)
                else:
                    why = "no printer address (set the host in Orca's printer settings or connect the Device tab)" if not MAPSTATE.get("base") else "popup off"
                if why == "cancelled":
                    neutralize_gcode(path)
                    return abort_result("WM Tool Map: cancelled in the mapping window, nothing is sent")
                if mapping is None and why != "skipped":
                    mapping = clean_map(MAPSTATE.get("map")) if MAPSTATE.get("write") else None
                if mapping is None:
                    return orca.ExecutionResult.success("WM Tool Map: no map written (" + (why or "off in the Device tab") + ")")
                native = bool(MAPSTATE.get("base")) and not printer_has_macro(MAPSTATE["base"], "WM_SET_TOOL_MAP")
                if not apply_tool_map(path, mapping, ctx.output_name, native):
                    return orca.ExecutionResult.success("WM Tool Map: the file already has a tool map")
                MAPSTATE["map"] = mapping
                return orca.ExecutionResult.success("WM Tool Map: " + ", ".join(f"T{t}->{v}" for t, v in enumerate(mapping)))
            except WmAbort:
                raise
            except Exception as exc:  # noqa: BLE001
                return orca.ExecutionResult.success("WM Tool Map skipped: " + repr(exc))


class WmDeviceTab(orca.pages.PagesPluginCapabilityBase):
    poller = None

    def get_name(self):
        return "WM Device"

    def get_ui(self):
        return PAGE

    def on_unload(self):
        try:
            if self.poller is not None:
                self.poller.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _post(self, data):
        try:
            self.post_message(data)
        except Exception:  # noqa: BLE001
            pass

    def _notify_more(self):
        """Orca notices for a lost connection (3 failed reads in a row), the way back, and Klipper error/shutdown. Never raises."""
        try:
            p = self.poller
            if p is None or not p.base:
                return
            lvl = orca.host.ui.NotificationLevel
            warn = getattr(lvl, "WarningNotificationLevel", None)
            reg = getattr(lvl, "RegularNotificationLevel", None)
            if p.fails >= 3 and not getattr(self, "_lost", False):
                self._lost = True
                if warn is not None:
                    orca.host.ui.push_notification(warn, "WM Device: connection to the printer lost")
            elif p.fails == 0 and getattr(self, "_lost", False):
                self._lost = False
                if reg is not None:
                    orca.host.ui.push_notification(reg, "WM Device: connection to the printer is back")
            ks = p.kstate if p.fails == 0 else ""
            old = getattr(self, "_kst", "")
            if ks and ks != old:
                self._kst = ks
                if ks in ("error", "shutdown") and warn is not None:
                    orca.host.ui.push_notification(warn, "WM Device: Klipper is " + ks)
        except Exception:  # noqa: BLE001
            pass

    def _notify_state(self):
        """Orca notification when a print ends, pauses or fails (state change seen by the poller). Never raises."""
        try:
            st = self.poller.print_state
            old = getattr(self, "_last_state", None)
            self._last_state = st
            if old == "printing" and st in ("complete", "error", "cancelled", "standby", "paused"):
                lvl = orca.host.ui.NotificationLevel
                level = getattr(lvl, "RegularNotificationLevel", None) if st == "complete" else getattr(lvl, "WarningNotificationLevel", None)
                if level is None:
                    return
                orca.host.ui.push_notification(level, "WM Device: print " + ("finished" if st == "complete" else st))
        except Exception:  # noqa: BLE001
            pass

    def on_message(self, message):
        try:
            if not isinstance(message, dict):
                return
            kind = message.get("type")
            if kind == "connect":
                if self.poller is None:
                    self.poller = Poller(self._post)
                base = base_url(message.get("addr"))
                if PERM["ok"] and _host_of(base) not in PERM["hosts"]:
                    # Orca only accepts a permission request while the plugin loads, and an address it was not told about
                    # prompts once per URL. Remember it, so the next start approves it in the single dialog.
                    remember_host(base)
                    set_last_host(_host_of(base))
                    self._post({"type": "defaults", "diag": _diag_text()})
                    self._post({"type": "approve", "host": _host_of(base),
                                "info": (PERM["req"] or "no request") + (" | read error: " + PERM["err"] if PERM["err"] else "")})
                    return
                MAPSTATE["base"] = base
                remember_host(base)
                set_last_host(_host_of(base))
                self._post({"type": "defaults", "diag": _diag_text()})
                self.poller.set_base(MAPSTATE["base"])
            elif kind == "forget":
                forget_host(base_url(message.get("addr")))
                if get_last_host() == _host_of(base_url(message.get("addr"))):
                    set_last_host("")
                if self.poller is not None and MAPSTATE.get("base") == base_url(message.get("addr")):
                    self.poller.set_base("")
                    MAPSTATE["base"] = ""
            elif kind == "defaults":
                ph = orca_print_host()
                remember_host(ph, auto=True)
                if _host_of(ph) in forgotten_hosts():
                    ph = ""
                gone = forgotten_hosts()
                saved = [h for h in known_hosts() if h not in gone]
                last = get_last_host()
                if last in gone:
                    last = ""
                if last and last not in saved:
                    saved.append(last)
                diag = _diag_text()
                self._post({"type": "defaults", "host": ph.replace("http://", "").replace(":7125", ""),
                            "hosts": saved, "last": last, "diag": diag})
            elif kind == "ping":
                if self.poller is not None:
                    self.poller.beat()
                    self._notify_state()
                    self._notify_more()
            elif kind == "probe":
                try:
                    txt = probe_orca_api()
                except Exception as exc:  # noqa: BLE001
                    txt = "probe failed: " + repr(exc)
                self._post({"type": "probe", "text": txt})
            elif kind == "orcafils":
                fils, err = read_orca_filaments()
                self._post({"type": "orcafils", "fils": fils, "err": err})
            elif kind == "mapstate":
                MAPSTATE["write"] = bool(message.get("write"))
                MAPSTATE["map"] = clean_map(message.get("map"))
            elif self.poller is None or not self.poller.base:
                return
            elif kind == "printfile":
                threading.Thread(target=self.poller.print_with_map, daemon=True, args=(str(message.get("file", "")),)).start()
            elif kind == "action":
                threading.Thread(target=self.poller.action, daemon=True, args=(message,)).start()
            elif kind == "history":
                threading.Thread(target=self.poller.history, daemon=True).start()
            elif kind == "timelapse":
                threading.Thread(target=self.poller.timelapse, daemon=True).start()
            elif kind == "deletetl":
                threading.Thread(target=self.poller.delete_timelapse, daemon=True, args=([str(x) for x in (message.get("files") or [])][:50],)).start()
            elif kind == "opentl":
                threading.Thread(target=self.poller.open_timelapse, daemon=True, args=(str(message.get("file", "")),)).start()
            elif kind == "deletehist":
                threading.Thread(target=self.poller.delete_history, daemon=True, args=([str(x) for x in (message.get("ids") or [])][:50],)).start()
            elif kind == "promptbtn":
                threading.Thread(target=self.poller.prompt_click, daemon=True, args=(str(message.get("script", "")),)).start()
            elif kind == "deletefiles":
                names = [str(x) for x in (message.get("files") or [])][:50]
                threading.Thread(target=self.poller.delete_files, daemon=True, args=(names,)).start()
            elif kind == "files":
                threading.Thread(target=self.poller.files, daemon=True).start()
            elif kind == "command":
                threading.Thread(target=self.poller.command, daemon=True,
                                 args=(str(message.get("name", "")), str(message.get("file", "")))).start()
            elif kind == "install_helper":
                user = str(message.get("user", "") or "")
                password = str(message.get("password", "") or "")
                threading.Thread(target=self._install_helper, daemon=True, args=(user, password)).start()
        except Exception:  # noqa: BLE001
            pass

    def _install_helper(self, user, password):
        """Runs on a background thread (see on_message's "install_helper"): SSHes into the
        printer's screen board and installs the optional wm-screen-sync-http helper, then reports
        back to the page. Never raises into the plugin's own message loop."""
        try:
            base = self.poller.base if self.poller is not None else ""
            ok, detail = install_screen_sync_helper(base, user, password)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "install failed: %r" % exc
        self._post({"type": "install_helper_result", "ok": ok, "detail": detail})


@orca.plugin
class WmDevicePlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(WmDeviceTab)
        if "WmToolMapStep" in globals():
            try:
                orca.register_capability(WmToolMapStep)
            except Exception:  # noqa: BLE001
                pass
        declare_permissions()

