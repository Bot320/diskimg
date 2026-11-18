# SPDX-License-Identifier: GPL-3.0-only
"""Headless test of the rescue start menu (GTK Broadway). Usage: python3 -I iso/tests/test_launcher.py [PNG]"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent / "profile/airootfs/usr/local/lib/rescue/launcher.py"
rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
n = 40 + os.getpid() % 50
while os.path.exists(f"{rt}/broadway{n + 1}.socket"):
    n += 1
bw = subprocess.Popen(["gtk4-broadwayd", f":{n}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=f":{n}", RESCUE_SHOW_ALL="1",
                  XKB_DEFAULT_LAYOUT="de")
STATE = os.path.join(tempfile.mkdtemp(), "rescue-kbd")
import importlib.util  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

spec = importlib.util.spec_from_file_location("launcher", LAUNCHER)
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)
L.kbd_state_file = lambda: STATE          # do not touch the real session state
results = []


def check(c, what):
    results.append(bool(c))
    print(("ok    " if c else "FAIL  ") + what, flush=True)


def wait(pred, t=10.0):
    ctx = GLib.MainContext.default()
    t0 = time.monotonic()
    while time.monotonic() - t0 < t:
        while ctx.pending():
            ctx.iteration(False)
        if pred():
            return True
        time.sleep(0.05)
    return False


def main():
    app = L.App()
    app.register(None)
    app.activate()
    w = app.win
    wait(lambda: False, 1.5)
    check(len(w.tiles) == len(L.APPS), f"tiles: {list(w.tiles)}")
    if len(sys.argv) > 1:
        ww, hh = w.get_width(), w.get_height()
        s = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(w).snapshot(s, ww, hh)
        node = s.to_node()
        if node is not None:
            w.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, ww, hh)).save_to_png(sys.argv[1])
            print("screenshot", sys.argv[1])
    # launching: a harmless command stands in for a program
    a = {"id": "x", "title": "Test", "cmd": ["sleep", "0.5"]}
    w.tiles["x"] = Gtk.Button(tooltip_text="Test")
    w.launch(a)
    check("x" in w.running and not w.tiles["x"].get_sensitive(), "program started, tile disabled while running")
    check(wait(lambda: "x" not in w.running, 5) and w.tiles["x"].get_sensitive(), "tile enabled again after exit")
    w.launch({"id": "y", "title": "Missing", "cmd": ["/nonexistent/program"]})
    w.tiles["y"] = Gtk.Button()
    check("y" not in w.running, "missing program -> message, no crash")
    check(L.nmcli_split(r"*:My\:Net:80:WPA2") == ["*", "My:Net", "80", "WPA2"], "nmcli escaped colon parsing")
    check(L.current_kbd() == "de" and w.kbd_name("de") == "Deutsch", "keyboard: Deutsch")
    # keyboard switch -> state file + exit code 10
    w.change_kbd("us")
    check(wait(lambda: w.get_visible_dialog() is not None, 3), "keyboard switch asks first")
    d = w.get_visible_dialog()
    d.emit("response", "ok")
    d.force_close()
    check(wait(lambda: app.exit_code == L.RESTART_SESSION, 3), "restart requested (exit code 10)")
    check(open(L.kbd_state_file()).read().strip() == "us", "new layout written for the session")
    print("LAUNCHER TEST " + ("PASSED" if all(results) else "FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        bw.terminate()
    sys.exit(rc)
