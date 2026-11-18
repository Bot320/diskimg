"""Headless GUI test (GTK Broadway backend, helper in non-root test mode).
Usage:  python3 -I tests/test_gui.py [BACKUP.dimg]
Starts gtk4-broadwayd on a free display, opens the app, walks through all pages and dialogs, and
(with BACKUP) runs a real check job through the GUI."""
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISKIMG_HELPER_TEST"] = "1"
_rt = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
_n = 40 + os.getpid() % 50
while os.path.exists(f"{_rt}/broadway{_n + 1}.socket"):     # display :N uses broadway{N+1}.socket
    _n += 1
DISPLAY = f":{_n}"
bw = subprocess.Popen(["gtk4-broadwayd", DISPLAY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.0)
os.environ["GDK_BACKEND"] = "broadway"
os.environ["BROADWAY_DISPLAY"] = DISPLAY

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from diskimg import gui as G  # noqa: E402

BACKUP = sys.argv[1] if len(sys.argv) > 1 else None
results = []


def check(cond, what):
    results.append((bool(cond), what))
    print(("ok    " if cond else "FAIL  ") + what, flush=True)


def wait(pred, timeout=30.0, step=0.1):
    """Run the main loop until pred() is true."""
    ctx = GLib.MainContext.default()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        while ctx.pending():
            ctx.iteration(False)
        if pred():
            return True
        time.sleep(step)
    return False


def visible_dialog(win):
    d = win.get_visible_dialog() if hasattr(win, "get_visible_dialog") else None
    return d


def main() -> int:
    app = G.App()
    app.register(None)
    app.activate()
    win = app.win
    orig_toast = win.toast
    win.toast = lambda text, timeout=5: (print("   toast:", text, flush=True), orig_toast(text, timeout))
    try:
        check(wait(lambda: win.stack.get_visible_child_name() == "setup", 30), "helper connected, setup page shown")
        check(wait(lambda: len(win.setup.devs) > 0, 20), f"devices listed ({len(win.setup.devs)})")
        s = win.setup
        for m in ("d2i", "d2d", "i2d", "chk", "brw"):
            s.set_mode(m)
            wait(lambda: False, 0.6)
            check(not s.start_btn.get_sensitive(), f"mode {m}: Start disabled without input "
                  f"('{s.summary.get_text()[:60]}')")
        s.set_mode("d2i")
        wait(lambda: False, 0.8)
        check(s.dst_path.get_text().startswith("/mnt/"), f"default backup name: {s.dst_path.get_text()}")
        # options visibility follows the engine
        s.comp.set_selected(2)
        wait(lambda: False, 0.4)
        check(not s.sparse.get_visible() and s.dst_path.get_text().endswith(".dimg"),
              "compression -> container format (.dimg), ddrescue options hidden")
        s.comp.set_selected(0)
        # network dialog
        win.network_from_header()
        check(wait(lambda: visible_dialog(win) is not None, 5), "network dialog opens")
        d = visible_dialog(win)
        d.server.set_text("")
        d.connect_clicked(None)
        check(wait(lambda: "server" in d.msg.get_text().lower(), 10), f"network: validation message ({d.msg.get_text()})")
        d.close()
        wait(lambda: visible_dialog(win) is None, 5)
        # file backup page
        s.set_mode("fbk")
        check(wait(lambda: win.stack.get_visible_child_name() == "files", 5), "file backup page")
        wait(lambda: False, 1.5)
        win.files.action("save")
        check(wait(lambda: "name" in win.files.msg.get_text().lower(), 10),
              f"file set validation: {win.files.msg.get_text()}")
        win.show_setup()
        # passphrase dialog: wrong then right
        res = {}
        G.passphrase_dialog(win, new=False, checker=lambda x: _done(x == "s3cret!!")).then(
            lambda ok, v: res.setdefault("v", v))
        wait(lambda: visible_dialog(win) is not None, 5)
        _answer(win, "wrong")
        check(wait(lambda: visible_dialog(win) is not None and "Wrong" in _dialog_text(win), 5),
              "wrong passphrase -> dialog again with message")
        _answer(win, "s3cret!!")
        check(wait(lambda: "v" in res, 5) and res["v"] == "s3cret!!", "right passphrase accepted")
        if BACKUP:
            s.set_mode("chk")
            s.src_path.set_text(BACKUP)
            check(wait(lambda: s.start_btn.get_sensitive(), 10), f"check job ready: {s.summary.get_text()}")
            check(wait(lambda: "instant browse" in s.src_info.get_text() or "uncompressed" in s.src_info.get_text(), 5),
                  f"source info: {s.src_info.get_text()[:70]}")
            win.start_job()
            check(wait(lambda: visible_dialog(win) is not None, 10), "confirmation dialog")
            dlg = visible_dialog(win)
            check("Confirm" in (dlg.get_heading() or ""), f"it is the confirmation ({dlg.get_heading()})")
            dlg.emit("response", "go")
            dlg.force_close()
            check(wait(lambda: win.stack.get_visible_child_name() == "run", 10), "run page shown")
            check(wait(lambda: not win.run_page.running, 120), "job finished")
            check("OK - backup is intact" in win.run_page.result.get_text(),
                  f"result in GUI: {win.run_page.result.get_text()}")
            buf = win.run_page.log.tv.get_buffer()
            log = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            check("All image files are intact" in log, "log shown in the GUI")
            check(len(win.run_page.item_cells) >= 1, f"item table rows: {len(win.run_page.item_cells)}")
            win.show_setup()
            # browse mode lists the partitions of the backup
            s.set_mode("brw")
            s.brw_src = None
            s.fill_brw()
            check(wait(lambda: len(s.brw_rows) > 0, 10), f"browse rows: {[r['id'] for r in s.brw_rows]}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        check(False, "exception")
    finally:
        win.closing = True
        win.helper.close()
        wait(lambda: not win.helper.alive, 30)
        check(not win.helper.alive, "helper exits when the GUI closes")
    fails = sum(1 for ok, _ in results if not ok)
    print("\nGUI TEST " + ("PASSED" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


def _done(v):
    p = G.Pending()
    GLib.timeout_add(300, lambda: (p.resolve(v), False)[1])
    return p


def _dialog_text(win):
    d = visible_dialog(win)
    if d is None:
        return ""
    child = d.get_extra_child()
    texts = []
    w = child.get_first_child() if child else None
    while w is not None:
        if isinstance(w, Gtk.Label):
            texts.append(w.get_text())
        w = w.get_next_sibling()
    return " ".join(texts)


def _answer(win, pw):
    d = visible_dialog(win)
    e = d.get_extra_child().get_first_child()
    e.set_text(pw)
    d.emit("response", "ok")
    d.force_close()                 # a real click closes the dialog after the response


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        bw.terminate()
    sys.exit(rc)
