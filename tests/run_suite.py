#!/usr/bin/env python3
"""Offline self-test suite for wm_device.py (the WM Device OrcaSlicer plugin).

What this is for: catching logic bugs in the parts of the plugin that do NOT need a live
printer or a real Orca install - string formatting, parsing, branch logic. It will NOT catch
anything that needs a real Moonraker/printer/Orca process (network timing, Orca's own plugin
loading, the SSH helper install, ACE behaviour, look-and-feel). Run it after every change to
wm_device.py, before sending the file or committing/pushing, as another check alongside
ast.parse() - not a replacement for testing on the real printer.

Usage:  python3 tests/run_suite.py /path/to/wm_device.py
        (defaults to ../wm_device.py, i.e. the file next to this tests/ folder)

No network calls are made. Exits 0 if every test passes, 1 otherwise, and prints a
pass/fail line per test so a failure points straight at what broke.
"""
import ast
import json
import os
import sys
import types
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
DEFAULT_TARGET = os.path.join(HERE, "..", "wm_device.py")

RESULTS = []  # (name, ok, detail)


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))


def load_module(path):
    """Exec's the real wm_device.py under a minimal stub orca module (same shape as the
    user's own _wm_fix_selftest*.py scripts) so every pure function/class becomes callable
    without a real Orca process. Returns the exec namespace (acts like a module's __dict__)."""
    orca = types.ModuleType("orca")
    orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=object)
    orca.host = types.SimpleNamespace()
    orca.plugin = lambda c: c
    orca.base = object
    orca.register_capability = lambda *a, **k: None
    orca.ExecutionResult = types.SimpleNamespace(success=lambda *a, **k: None)
    sys.modules["orca"] = orca

    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    ast.parse(src)  # fail fast with a normal SyntaxError/traceback if the file itself is broken
    ns = {"__name__": "wm_device_under_test"}
    exec(compile(src, path, "exec"), ns)  # noqa: S102 - this is the whole point of this file
    return ns


def fake_urlopen(responses):
    """responses: dict url-substring -> bytes body (or callable(req)->bytes). Returns a context-
    manager-compatible fake matching urllib.request.urlopen's usage in wm_device.py."""

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key, val in responses.items():
            if key in url:
                body = val(req) if callable(val) else val
                return FakeResp(body)
        raise AssertionError("fake_urlopen: no matching response for " + url)

    return _urlopen


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    target = os.path.abspath(target)
    print("Testing:", target)
    ns = load_module(target)
    check("file imports under a stub orca module", True)

    # ---- gcode_filament_usage() / mark_filament_usage() - F-257/F-258 (filament usage from the
    # sliced file's own footer stats line, not the header) -----------------------------------
    gfu = ns["gcode_filament_usage"]
    mfu = ns["mark_filament_usage"]

    two_of_four = os.path.join(FIXTURES, "two_of_four_used.gcode")
    usage = gfu(two_of_four)
    check("gcode_filament_usage finds the footer line (synthetic fixture)",
          usage == [120.5, 85.2, 0.0, 0.0], detail=str(usage))

    all_four = os.path.join(FIXTURES, "all_four_used.gcode")
    usage_all = gfu(all_four)
    check("gcode_filament_usage: all-4-used fixture parses",
          usage_all == [100.0, 50.0, 25.0, 10.0], detail=str(usage_all))

    no_line = os.path.join(FIXTURES, "no_usage_line.gcode")
    usage_none = gfu(no_line)
    check("gcode_filament_usage returns None when the line is missing (fail-open input)",
          usage_none is None, detail=str(usage_none))

    real_file = os.path.join(FIXTURES, "real_sample_all_used.gcode")
    if os.path.exists(real_file):
        usage_real = gfu(real_file)
        check("gcode_filament_usage parses a REAL exported OrcaSlicer file",
              usage_real is not None and len(usage_real) == 4, detail=str(usage_real))
    else:
        check("gcode_filament_usage real-file fixture present", False, "fixtures/real_sample_all_used.gcode missing")

    fils4 = [{"index": i, "colour": "#000000", "type": "PLA", "name": ""} for i in range(4)]
    marked = mfu([dict(f) for f in fils4], two_of_four)
    check("mark_filament_usage flags T0/T1 used, T2/T3 not (the user's reported case)",
          [f["used"] for f in marked] == [True, True, False, False], detail=str(marked))

    marked_all = mfu([dict(f) for f in fils4], all_four)
    check("mark_filament_usage: nothing hidden when every slot is used",
          all(f["used"] for f in marked_all), detail=str(marked_all))

    marked_none = mfu([dict(f) for f in fils4], no_line)
    check("mark_filament_usage fails open (shows everything) when it can't read usage",
          all(f["used"] for f in marked_none), detail=str(marked_none))

    marked_nonfile = mfu([dict(f) for f in fils4], "/no/such/file.gcode")
    check("mark_filament_usage fails open on a missing/unreadable file",
          all(f["used"] for f in marked_nonfile), detail=str(marked_nonfile))

    # ---- has_tmt1-style factory/vanilla detection - B-094 (don't show the factory helper card
    # on a vanilla Klipper printer just because tmt1.ini exists there too) -------------------
    vanilla_objects = ["toolhead", "gcode_macro _ZR_SET_TOOL_INFO", "gcode_macro WM_CHAMBER_LIGHT"]
    factory_objects = ["toolhead", "gcode_macro WM_CHAMBER_LIGHT"]
    check("factory/vanilla signal: vanilla Klipper (_ZR_SET_TOOL_INFO present) is NOT treated as factory",
          ("gcode_macro _ZR_SET_TOOL_INFO" not in vanilla_objects) is False)
    check("factory/vanilla signal: factory board (_ZR_SET_TOOL_INFO absent) IS treated as factory",
          ("gcode_macro _ZR_SET_TOOL_INFO" not in factory_objects) is True)

    # ---- moonraker_power() - B-096 (read/set a [power chamber_light] device's real state) ----
    mp = ns["moonraker_power"]
    real_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = fake_urlopen({
            "device_power/device?device=chamber_light": json.dumps({"result": {"chamber_light": "on"}}).encode(),
        })
        st = mp("http://10.0.0.1:7125", "chamber_light")
        check("moonraker_power reads a GET device-status response", st == "on", detail=str(st))

        urllib.request.urlopen = fake_urlopen({
            "device_power/device": json.dumps({"chamber_light": "off"}).encode(),
        })
        st2 = mp("http://10.0.0.1:7125", "chamber_light", action="toggle")
        check("moonraker_power reads a POST action response (unwrapped, no 'result' key)",
              st2 == "off", detail=str(st2))

        urllib.request.urlopen = fake_urlopen({
            "device_power/device": json.dumps({"result": {"chamber_light": "sideways"}}).encode(),
        })
        st3 = mp("http://10.0.0.1:7125", "chamber_light")
        check("moonraker_power rejects a value that isn't on/off",
              st3 is None, detail=str(st3))

        def _raise(req):
            raise OSError("connection refused")
        urllib.request.urlopen = fake_urlopen({"device_power": _raise})
        st4 = mp("http://10.0.0.1:7125", "chamber_light")
        check("moonraker_power returns None (not an exception) when the printer is unreachable",
              st4 is None, detail=str(st4))
    finally:
        urllib.request.urlopen = real_urlopen

    # ---- WANT list sanity: both macros this file branches on must actually be subscribed ----
    want = ns["WANT"]
    check("WANT subscribes gcode_macro WM_CHAMBER_LIGHT (needed for the vanilla light-state poll)",
          "gcode_macro WM_CHAMBER_LIGHT" in want)
    check("WANT subscribes gcode_macro _ZR_SET_TOOL_INFO (needed for the factory/vanilla signal)",
          "gcode_macro _ZR_SET_TOOL_INFO" in want)

    # ---- clean up fixture pollution from a fake OSError test, if any ------------------------

    print()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        line = "[%s] %s" % (mark, name)
        if not ok and detail:
            line += "  ->  " + detail
        print(line)
    print()
    print("%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
