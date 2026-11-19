# SPDX-License-Identifier: GPL-3.0-only
"""Start menu of the rescue USB.

Runs full screen inside the cage kiosk compositor (see rescue-session) as the live user. Programs
started from here open on top of it; when they are closed, the start menu is visible again.
Exit code 10 asks rescue-session to restart the graphical session (after a keyboard change).
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

RESTART_SESSION = 10
APPS = [
    {"id": "diskimg", "title": "diskimg", "icon": "io.github.bot320.diskimg",
     "subtitle": "Back up, restore, clone, check and browse disks and partitions",
     "cmd": ["diskimg", "--gui"]},
    {"id": "bootrepair", "title": "BootRepair", "icon": "io.github.bootrepair",
     "subtitle": "Find out why Windows does not start, and repair it",
     "cmd": ["bootrepair"]},
    {"id": "gparted", "title": "GParted", "icon": "gparted",
     "subtitle": "Create, resize and delete partitions",
     "cmd": ["rescue-root-app", "gparted"], "needs": "gparted"},
    {"id": "files", "title": "Files", "icon": "org.gnome.Nautilus",
     "subtitle": "All drives of this computer: open, copy files, mount and unmount",
     "cmd": ["nautilus", "--new-window", "computer:///"], "needs": "nautilus"},
    {"id": "terminal", "title": "Terminal", "icon": "foot",
     "subtitle": "Command line (sudo works without a password)",
     "cmd": ["foot"]},
    {"id": "diskimg-tui", "title": "diskimg (text)", "icon": "utilities-terminal",
     "subtitle": "The text interface of diskimg",
     "cmd": ["foot", "--title", "diskimg", "sudo", "diskimg", "--tui"], "needs": "diskimg"},
]
KEYBOARDS = [("de", "Deutsch", "de-latin1"), ("ch", "Deutsch (Schweiz)", "de_CH-latin1"),
             ("us", "English (US)", "us"), ("gb", "English (UK)", "uk"), ("fr", "Français", "fr-latin1"),
             ("es", "Español", "es"), ("it", "Italiano", "it"), ("nl", "Nederlands", "nl")]
CSS = """
.tile { padding: 22px 18px; border-radius: 16px; min-width: 260px; }
.tile image { margin-bottom: 6px; }
.tile .subtitle { opacity: 0.7; }
.statusbar { padding: 6px 14px; }
"""


def kbd_state_file() -> str:
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "rescue-kbd")


def current_kbd() -> str:
    return os.environ.get("XKB_DEFAULT_LAYOUT") or "us"


def run_async(argv: list[str], done) -> None:
    """Run a command without blocking the window; done(ok, stdout, stderr) on the main loop."""
    try:
        p = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as e:
        done(False, "", e.message)
        return

    def fin(proc, res):
        try:
            _ok, out, err = proc.communicate_utf8_finish(res)
        except GLib.Error as e:
            done(False, "", e.message)
            return
        done(proc.get_successful(), out or "", err or "")
    p.communicate_utf8_async(None, None, fin)


def nmcli_split(line: str) -> list[str]:
    """Split one line of 'nmcli -t' output (':' separated, '\\:' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class WifiDialog(Adw.Dialog):
    def __init__(self, win: "Launcher"):
        super().__init__(title="Network", content_width=560, content_height=620)
        self.win = win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Search again")
        self.rescan.connect("clicked", lambda *_: self.scan(rescan=True))
        hb.pack_start(self.rescan)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        self.status_group = Adw.PreferencesGroup(title="Connection")
        self.status_row = Adw.ActionRow(title="checking …")
        self.status_group.add(self.status_row)
        page.add(self.status_group)
        self.list_group = Adw.PreferencesGroup(title="Wi-Fi networks",
                                               description="A cable connection works without any setup.")
        page.add(self.list_group)
        self.rows: list[Gtk.Widget] = []
        tv.set_content(page)
        self.set_child(tv)
        self.scan(rescan=False)

    def set_rows(self, rows: list[Gtk.Widget]) -> None:
        for r in self.rows:
            self.list_group.remove(r)
        self.rows = rows
        for r in rows:
            self.list_group.add(r)

    def scan(self, rescan: bool) -> None:
        if not shutil.which("nmcli"):
            self.status_row.set_title("NetworkManager is not available")
            return
        self.status_row.set_title(self.win.net_text or "checking …")
        self.rescan.set_sensitive(False)
        self.set_rows([Adw.ActionRow(title="searching for networks …")])
        run_async(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan",
                   "yes" if rescan else "auto"], self.got_list)

    def got_list(self, ok: bool, out: str, err: str) -> None:
        self.rescan.set_sensitive(True)
        if not ok:
            self.set_rows([Adw.ActionRow(title="No Wi-Fi adapter found" if "No Wi-Fi device" in err
                                         or "not found" in err.lower() else GLib.markup_escape_text(
                                             err.strip() or "Wi-Fi is not available"))])
            return
        nets: dict[str, tuple[bool, int, str]] = {}
        for line in out.splitlines():
            f = nmcli_split(line)
            if len(f) < 4 or not f[1]:
                continue
            inuse, ssid, signal, sec = f[0] == "*", f[1], int(f[2] or 0), f[3]
            old = nets.get(ssid)
            if old is None or signal > old[1] or inuse:
                nets[ssid] = (inuse or (old[0] if old else False), signal, sec)
        rows = []
        for ssid, (inuse, signal, sec) in sorted(nets.items(), key=lambda kv: (not kv[1][0], -kv[1][1])):
            icon = ("network-wireless-signal-excellent-symbolic" if signal >= 75 else
                    "network-wireless-signal-good-symbolic" if signal >= 50 else
                    "network-wireless-signal-ok-symbolic" if signal >= 25 else
                    "network-wireless-signal-weak-symbolic")
            row = Adw.ActionRow(title=GLib.markup_escape_text(ssid), activatable=True,
                                subtitle=("connected" if inuse else "") + ("" if not sec or sec == "--"
                                                                            else ("   " if inuse else "") + sec))
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            if sec and sec != "--":
                row.add_suffix(Gtk.Image.new_from_icon_name("system-lock-screen-symbolic"))
            if inuse:
                b = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_: self.disconnect())
                row.add_suffix(b)
            else:
                row.connect("activated", lambda _r, s=ssid, secured=bool(sec and sec != "--"): self.connect_to(s, secured))
            rows.append(row)
        self.set_rows(rows or [Adw.ActionRow(title="No Wi-Fi networks found (or no Wi-Fi adapter)")])

    def connect_to(self, ssid: str, secured: bool) -> None:
        if not secured:
            self.do_connect(ssid, "")
            return
        d = Adw.AlertDialog(heading=f"Connect to {ssid}", body="Enter the Wi-Fi password.")
        e = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
        d.set_extra_child(e)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Connect")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, rid: rid == "ok" and self.do_connect(ssid, e.get_text()))
        d.present(self.win)
        e.grab_focus()

    def do_connect(self, ssid: str, pw: str) -> None:
        self.status_row.set_title(f"connecting to {GLib.markup_escape_text(ssid)} …")
        argv = ["nmcli", "device", "wifi", "connect", ssid] + (["password", pw] if pw else [])

        def done(ok, out, err):
            if ok:
                self.win.toast(f"Connected to {ssid}")
            else:
                self.win.toast("Could not connect: " + (err.strip().splitlines() or ["unknown error"])[-1])
            self.win.refresh_network()
            self.scan(rescan=False)
        run_async(argv, done)

    def disconnect(self) -> None:
        def got(ok, out, err):
            devs = [nmcli_split(x) for x in out.splitlines()]
            wifi = [d[0] for d in devs if len(d) >= 3 and d[1] == "wifi" and d[2] == "connected"]
            for dev in wifi:
                run_async(["nmcli", "device", "disconnect", dev], lambda *_: (self.win.refresh_network(),
                                                                             self.scan(rescan=False)))
        run_async(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], got)


class Launcher(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Rescue USB", default_width=1280, default_height=800)
        self.running: dict[str, Gio.Subprocess] = {}
        self.tiles: dict[str, Gtk.Button] = {}
        self.net_text = ""
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        hb.set_title_widget(Adw.WindowTitle(title="Rescue USB",
                                            subtitle="Everything runs from the USB stick and RAM"))
        self.net_btn = Gtk.Button()
        self.net_box = Adw.ButtonContent(icon_name="network-offline-symbolic", label="Network")
        self.net_btn.set_child(self.net_box)
        self.net_btn.connect("clicked", lambda *_: WifiDialog(self).present(self))
        hb.pack_start(self.net_btn)
        kb = Gtk.MenuButton(child=Adw.ButtonContent(icon_name="input-keyboard-symbolic",
                                                    label=self.kbd_name(current_kbd())),
                            tooltip_text="Keyboard layout")
        menu = Gio.Menu()
        for code, name, _vc in KEYBOARDS:
            menu.append(name, f"win.kbd::{code}")
        kb.set_menu_model(menu)
        act = Gio.SimpleAction.new("kbd", GLib.VariantType.new("s"))
        act.connect("activate", lambda _a, v: self.change_kbd(v.get_string()))
        self.add_action(act)
        hb.pack_end(self.power_button())
        hb.pack_end(kb)
        tv.add_top_bar(hb)

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=3,
                           min_children_per_line=1, column_spacing=18, row_spacing=18,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        for a in APPS:
            need = a.get("needs") or a["cmd"][0]
            if not shutil.which(need) and not os.environ.get("RESCUE_SHOW_ALL"):
                continue
            flow.append(self.tile(a))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=36, margin_bottom=24,
                      margin_start=24, margin_end=24)
        box.append(Gtk.Label(label="What do you want to do?", css_classes=["title-1"]))
        box.append(flow)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(box)
        tv.set_content(sw)
        foot = Gtk.Label(label="Nothing is installed on this computer. Ctrl+Alt+F2 opens a text console "
                               "(user live, sudo without a password).", css_classes=["dim-label", "statusbar"],
                         wrap=True)
        tv.add_bottom_bar(foot)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)
        self.refresh_network()
        GLib.timeout_add_seconds(5, lambda: (self.refresh_network(), True)[1])
        GLib.idle_add(lambda: (self.auto_scale(), False)[1])

    # ---------------------------------------------------------------- tiles
    def tile(self, a: dict) -> Gtk.Button:
        b = Gtk.Button(css_classes=["card", "tile"], tooltip_text=" ".join(a["cmd"]))
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        icon = a["icon"] if theme.has_icon(a["icon"]) else "application-x-executable"
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(72)
        v.append(img)
        v.append(Gtk.Label(label=a["title"], css_classes=["title-3"]))
        sub = Gtk.Label(label=a["subtitle"], css_classes=["subtitle"], wrap=True, justify=Gtk.Justification.CENTER,
                        max_width_chars=30)
        sub.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        v.append(sub)
        b.set_child(v)
        b.connect("clicked", lambda *_: self.launch(a))
        self.tiles[a["id"]] = b
        return b

    def launch(self, a: dict) -> None:
        if a["id"] in self.running:
            self.toast(f"{a['title']} is already running.")
            return
        try:
            p = Gio.Subprocess.new(a["cmd"], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.toast(f"{a['title']} could not be started: {e.message}")
            return
        self.running[a["id"]] = p
        self.tiles[a["id"]].set_sensitive(False)

        def ended(proc, res):
            try:
                proc.wait_finish(res)
            except GLib.Error:
                pass
            self.running.pop(a["id"], None)
            self.tiles[a["id"]].set_sensitive(True)
            if proc.get_if_exited() and proc.get_exit_status() not in (0, 130):
                self.toast(f"{a['title']} ended with an error (code {proc.get_exit_status()}).")
        p.wait_async(None, ended)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=6))

    # ---------------------------------------------------------------- network
    def refresh_network(self) -> None:
        if not shutil.which("nmcli"):
            self.net_box.set_label("Network")
            return

        def got(ok, out, err):
            wired = wifi = None
            for line in out.splitlines():
                f = nmcli_split(line)
                if len(f) >= 3 and f[1].startswith("connected"):
                    if f[0] == "ethernet":
                        wired = f[2]
                    elif f[0] == "wifi":
                        wifi = f[2]
            if wifi:
                self.net_text = f"Wi-Fi: {wifi}"
                icon = "network-wireless-signal-excellent-symbolic"
            elif wired:
                self.net_text = "Wired network"
                icon = "network-wired-symbolic"
            else:
                self.net_text = "No network"
                icon = "network-offline-symbolic"
            self.net_box.set_label(self.net_text)
            self.net_box.set_icon_name(icon)
        run_async(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"], got)

    # ---------------------------------------------------------------- keyboard
    @staticmethod
    def kbd_name(code: str) -> str:
        return next((n for c, n, _v in KEYBOARDS if c == code), code)

    def change_kbd(self, code: str) -> None:
        if code == current_kbd():
            return
        running = ", ".join(self.tiles[i].get_tooltip_text() or i for i in self.running)
        body = "The start screen restarts with the new keyboard layout."
        if running:
            body += " Programs that are still open are closed."
        d = Adw.AlertDialog(heading=f"Keyboard: {self.kbd_name(code)}", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Switch")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.set_close_response("cancel")

        def resp(_d, rid):
            if rid != "ok":
                return
            try:
                with open(kbd_state_file(), "w") as f:
                    f.write(code + "\n")
            except OSError as e:
                self.toast(f"Could not switch: {e}")
                return
            self.get_application().exit_code = RESTART_SESSION
            self.get_application().quit()
        d.connect("response", resp)
        d.present(self)

    # ---------------------------------------------------------------- power
    def power_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        menu.append("Restart", "win.power::reboot")
        menu.append("Shut down", "win.power::poweroff")
        act = Gio.SimpleAction.new("power", GLib.VariantType.new("s"))
        act.connect("activate", lambda _a, v: self.power(v.get_string()))
        self.add_action(act)
        return Gtk.MenuButton(icon_name="system-shutdown-symbolic", menu_model=menu, tooltip_text="Shut down")

    def power(self, what: str) -> None:
        verb = "Restart" if what == "reboot" else "Shut down"
        body = "Remove the USB stick when the screen goes dark." if what == "poweroff" else \
            "Remove the USB stick now if the computer should start Windows or Linux."
        if self.running:
            body = "Programs are still open. Close them first so that running jobs are finished cleanly.\n\n" + body
        d = Adw.AlertDialog(heading=f"{verb} the computer?", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", verb)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")
        d.connect("response", lambda _d, rid: rid == "ok" and run_async(
            ["systemctl", what], lambda ok, o, e: ok or self.toast(f"{verb} failed: {e.strip()}")))
        d.present(self)

    # ---------------------------------------------------------------- display
    def auto_scale(self) -> None:
        """High-resolution laptop screens: use scale 2 so that text is readable."""
        if not shutil.which("wlr-randr") or os.environ.get("RESCUE_NO_SCALE"):
            return

        def got(ok, out, err):
            if not ok:
                return
            try:
                outputs = json.loads(out)
            except ValueError:
                return
            for o in outputs:
                if not o.get("enabled"):
                    continue
                mode = next((m for m in o.get("modes", []) if m.get("current")), None)
                phys = o.get("physical_size") or {}
                mm = phys.get("width") or 0
                if not mode or mm <= 0 or float(o.get("scale") or 1) != 1.0:
                    continue
                dpi = mode["width"] / (mm / 25.4)
                if dpi >= 170:
                    run_async(["wlr-randr", "--output", o["name"], "--scale", "2"], lambda *_: None)
        run_async(["wlr-randr", "--json"], got)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.bot320.RescueStart",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.exit_code = 0
        self.win: Launcher | None = None

    def do_activate(self) -> None:
        if self.win is None:
            self.win = Launcher(self)
        self.win.present()


def main() -> int:
    app = App()
    rc = app.run([sys.argv[0]])
    return app.exit_code or rc


if __name__ == "__main__":
    sys.exit(main())
