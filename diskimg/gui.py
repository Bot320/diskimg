# SPDX-License-Identifier: GPL-3.0-only
"""diskimg GTK4 / libadwaita user interface.

Runs as the desktop user. Everything that needs root (reading and writing disks, mounting) is done
by diskimg-helper, which this program starts once through pkexec and talks to over a pipe
(see helper.py). The disk logic itself lives in engine.py and is shared with the text UI.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from rich.markup import escape as rich_escape  # noqa: E402
from rich.text import Text  # noqa: E402

from . import engine as E  # noqa: E402

APP_ID = "io.github.bot320.diskimg"
HELPER = E.PKG_ROOT / "libexec" / "diskimg-helper"

COLORS = {"error": "#e01b24", "warning": "#c88800", "success": "#26a269", "accent": "#3584e4",
          "red": "#e01b24", "yellow": "#c88800", "green": "#26a269", "magenta": "#c061cb",
          "cyan": "#2190a4", "blue": "#3584e4"}
STEP_ICONS = {"pending": "content-loading-symbolic", "run": "media-playback-start-symbolic",
              "done": "object-select-symbolic", "skip": "action-unavailable-symbolic",
              "fail": "dialog-error-symbolic", "warn": "dialog-warning-symbolic"}
MODE_ICONS = {"d2i": "document-save-symbolic", "d2d": "edit-copy-symbolic",
              "i2d": "document-revert-symbolic", "chk": "emblem-ok-symbolic",
              "brw": "folder-open-symbolic", "fbk": "folder-documents-symbolic"}


# ----------------------------------------------------------------------------
#  Markup: the engine speaks Rich markup ([b $error]...[/]); GTK wants Pango markup
# ----------------------------------------------------------------------------
def _style_attrs(style: str) -> dict:
    a: dict[str, str] = {}
    for tok in style.replace(",", " ").split():
        t = tok.lower().lstrip("$")
        if t in ("b", "bold"):
            a["weight"] = "bold"
        elif t in ("i", "italic"):
            a["style"] = "italic"
        elif t == "dim":
            a["alpha"] = "65%"
        elif t in COLORS:
            a["foreground"] = COLORS[t]
        elif t.startswith("#") and len(t) in (4, 7):
            a["foreground"] = t
    return a


def pango(markup: str) -> str:
    """Rich markup -> Pango markup (unknown styles are dropped, text is always escaped)."""
    try:
        t = Text.from_markup(E.plain_style_vars(markup))
    except Exception:  # noqa: BLE001
        return GLib.markup_escape_text(E.strip_markup(markup))
    plain = t.plain
    n = len(plain)
    attrs: list[dict] = [{} for _ in range(n)]
    for sp in t.spans:
        a = _style_attrs(str(sp.style))
        for i in range(max(0, sp.start), min(sp.end, n)):
            attrs[i] = {**attrs[i], **a}
    out, i = [], 0
    while i < n:
        j = i
        while j < n and attrs[j] == attrs[i]:
            j += 1
        seg = GLib.markup_escape_text(plain[i:j])
        if attrs[i]:
            out.append("<span " + " ".join(f'{k}="{v}"' for k, v in sorted(attrs[i].items()))
                       + f">{seg}</span>")
        else:
            out.append(seg)
        i = j
    return "".join(out)


# ----------------------------------------------------------------------------
#  Small "promise" + generator flows, so multi-step dialogs read top to bottom
# ----------------------------------------------------------------------------
class FlowError(Exception):
    pass


class Pending:
    def __init__(self):
        self._cbs = []
        self._res = None

    def resolve(self, value=None) -> None:
        self._finish((True, value))

    def reject(self, err) -> None:
        self._finish((False, err))

    def _finish(self, res) -> None:
        if self._res is not None:
            return
        self._res = res
        for cb in self._cbs:
            cb(*res)

    def then(self, cb) -> None:
        if self._res is not None:
            cb(*self._res)
        else:
            self._cbs.append(cb)


def run_flow(gen, on_error) -> None:
    """Drive a generator that yields Pending objects; their result is sent back in."""
    def step(ok=True, value=None):
        try:
            p = gen.send(value) if ok else gen.throw(FlowError(str(value)))
        except StopIteration:
            return
        except FlowError as e:
            on_error(str(e))
            return
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            on_error(f"{type(e).__name__}: {e}")
            return
        p.then(lambda ok2, v: GLib.idle_add(lambda: (step(ok2, v), False)[1]))
    step()


# ----------------------------------------------------------------------------
#  Connection to the privileged helper
# ----------------------------------------------------------------------------
class Helper:
    def __init__(self, on_event, on_exit):
        self.on_event = on_event
        self.on_exit = on_exit
        self.proc: Gio.Subprocess | None = None
        self.out: Gio.OutputStream | None = None
        self.inp: Gio.DataInputStream | None = None
        self.n = 0
        self.waiting: dict[int, Pending] = {}
        self.ready = Pending()

    def start(self) -> None:
        if os.environ.get("DISKIMG_HELPER_TEST") == "1":     # development: no root, limited
            argv = [sys.executable, "-I", str(HELPER)]
        else:
            argv = ["pkexec", "--disable-internal-agent", str(HELPER)]
        self.ready = Pending()
        try:
            self.proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDIN_PIPE
                                           | Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error as e:
            self.ready.reject(f"Cannot start the helper: {e.message}")
            return
        self.out = self.proc.get_stdin_pipe()
        self.inp = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.inp.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)
        self.proc.wait_async(None, self._on_exit)

    @property
    def alive(self) -> bool:
        return self.proc is not None

    def _on_line(self, stream, res) -> None:
        try:
            line, _n = stream.read_line_finish_utf8(res)
        except GLib.Error:
            line = None
        if line is None:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        try:
            self._dispatch(msg)
        except Exception:  # noqa: BLE001 - never let one bad message stop the reading
            import traceback
            traceback.print_exc()
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _dispatch(self, msg) -> None:
        if isinstance(msg, dict):
            if msg.get("type") == "reply":
                p = self.waiting.pop(msg.get("id"), None)
                if p is not None:
                    if msg.get("ok"):
                        p.resolve(msg.get("result"))
                    else:
                        p.reject(msg.get("error") or "error")
            elif msg.get("event") == "ready":
                self.ready.resolve(msg)
            elif msg.get("type") == "event":
                self.on_event(msg)

    def _on_exit(self, proc, res) -> None:
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        code = proc.get_exit_status() if proc.get_if_exited() else -1
        self.proc = None
        for p in list(self.waiting.values()):
            p.reject("The helper stopped.")
        self.waiting.clear()
        self.ready.reject("Authorization was refused or cancelled." if code in (126, 127)
                          else f"The helper stopped (code {code}).")
        self.on_exit(code)

    def call(self, cmd: str, **args) -> Pending:
        p = Pending()
        if self.proc is None or self.out is None:
            p.reject("Not connected to the helper.")
            return p
        self.n += 1
        self.waiting[self.n] = p
        data = (json.dumps({"id": self.n, "cmd": cmd, "args": args}) + "\n").encode()
        try:
            self.out.write_all(data, None)
            self.out.flush(None)
        except GLib.Error as e:
            self.waiting.pop(self.n, None)
            p.reject(f"Helper connection lost: {e.message}")
        return p

    def close(self) -> None:
        if self.out is not None:
            try:
                self.out.close(None)
            except GLib.Error:
                pass
            self.out = None


# ----------------------------------------------------------------------------
#  Widgets
# ----------------------------------------------------------------------------
def label(text: str = "", markup: bool = False, xalign: float = 0.0, css: tuple = (), wrap: bool = False,
          selectable: bool = False, width: int = -1, ellipsize: bool = False,
          hexpand: bool = False) -> Gtk.Label:
    lb = Gtk.Label(xalign=xalign, hexpand=hexpand)
    if markup:
        lb.set_markup(text)
    else:
        lb.set_text(text)
    for c in css:
        lb.add_css_class(c)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if selectable:
        lb.set_selectable(True)
    if width > 0:
        lb.set_width_chars(width)
        lb.set_max_width_chars(width)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    return lb


def clear_box(box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class DeviceList(Gtk.Box):
    """Block devices as a selectable table."""
    # (title, minimum width in characters, expands): narrow minimums so that the window also fits
    # small screens (e.g. 1024 px on the rescue USB); long texts are shortened with "…"
    COLS = (("Device", 11, False), ("Size", 8, False), ("Type", 4, False), ("Model / Label", 8, True),
            ("FS", 6, False), ("Used", 9, False), ("Bus", 5, False), ("Status", 8, False))

    def __init__(self, on_select):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_select = on_select
        self.groups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in self.COLS]
        head = self._row_box([f"<b>{c[0]}</b>" for c in self.COLS], markup=True)
        head.add_css_class("dim-label")
        head.set_margin_start(30)
        self.append(head)
        self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lb.add_css_class("boxed-list")
        self.lb.connect("row-activated", self._activated)
        sw = Gtk.ScrolledWindow(vexpand=True, min_content_height=150)
        sw.set_child(self.lb)
        self.append(sw)
        self.used_labels: dict[str, Gtk.Label] = {}

    def _row_box(self, cells, markup=False) -> Gtk.Box:
        box = Gtk.Box(spacing=8, margin_top=4, margin_bottom=4, margin_end=8)
        for (name, width, expand), g, txt in zip(self.COLS, self.groups, cells):
            lb = label(txt, markup=markup, ellipsize=True, hexpand=expand)
            lb.set_width_chars(width)
            lb.set_max_width_chars(40 if expand else width + 6)
            g.add_widget(lb)
            box.append(lb)
        return box

    def set_devices(self, devs: list[dict], selected: str | None, other: str | None,
                    usage: dict[str, str]) -> None:
        self.lb.remove_all()
        self.used_labels = {}
        by_path = {d["path"]: d for d in devs}

        def related(a, b):
            if not a or not b or a not in by_path or b not in by_path:
                return False
            if a == b:
                return True
            for x, y in ((a, b), (b, a)):
                p = by_path[x].get("parent")
                while p:
                    if p == y:
                        return True
                    p = by_path.get(p, {}).get("parent")
            return False
        for d in devs:
            if not d["selectable"]:
                continue
            if d["busy"]:
                status = f'<span foreground="{COLORS["error"]}">in use</span>'
            elif d["ro"]:
                status = f'<span foreground="{COLORS["warning"]}">read-only</span>'
            else:
                status = f'<span foreground="{COLORS["success"]}">ok</span>'
            if related(other, d["path"]):
                status = f'<span foreground="{COLORS["magenta"]}">other side</span>'
            name = ("   " * d["depth"] + ("└ " if d["depth"] else "")) + d["name"]
            fs = d["fstype"] or (f"[{d['pttype']}]" if d["pttype"] else "-")
            cells = [GLib.markup_escape_text(x) for x in
                     (name, E.human(d["size"]), d["kind"], d["desc"], fs,
                      ("-" if d["fstype"] == "swap" or (d["has_children"] and not d["fstype"])
                       else usage.get(d["path"], "?" if not d["mounts"] else "...")),
                      d["tran"] or ("usb/rm" if d["rm"] else "-"))] + [status]
            box = self._row_box(cells, markup=True)
            self.used_labels[d["path"]] = box.get_first_child().get_next_sibling().get_next_sibling() \
                .get_next_sibling().get_next_sibling().get_next_sibling()
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_pixel_size(16)
            check.set_opacity(1.0 if d["path"] == selected else 0.0)
            outer = Gtk.Box(spacing=6, margin_start=6)
            outer.append(check)
            outer.append(box)
            row = Gtk.ListBoxRow(activatable=True)
            row.set_child(outer)
            row.path = d["path"]
            if d["path"] == selected:
                row.add_css_class("accent")
            row.set_tooltip_text(d["busy"] or d["long"])
            self.lb.append(row)

    def set_usage(self, path: str, text: str) -> None:
        lb = self.used_labels.get(path)
        if lb is not None:
            lb.set_text(text)

    def _activated(self, _lb, row) -> None:
        self.on_select(row.path)


class LogView(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__(vexpand=True, min_content_height=120)
        self.tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                               wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8,
                               top_margin=6, bottom_margin=6)
        self.set_child(self.tv)

    def clear(self) -> None:
        self.tv.get_buffer().set_text("")

    def add(self, markup: str) -> None:
        buf = self.tv.get_buffer()
        adj = self.get_vadjustment()
        at_end = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        buf.insert_markup(buf.get_end_iter(), pango(markup) + "\n", -1)
        if at_end:
            GLib.idle_add(lambda: (adj.set_value(adj.get_upper()), False)[1])


# ----------------------------------------------------------------------------
#  Dialogs (each returns a Pending)
# ----------------------------------------------------------------------------
def alert(parent, heading: str, body: str, responses, default: str | None = None,
          extra: Gtk.Widget | None = None, markup: bool = False) -> Pending:
    """responses: list of (id, label, appearance) with appearance in '', 'suggested', 'destructive'.
    Resolves with the chosen id, or None for cancel/close."""
    p = Pending()
    d = Adw.AlertDialog(heading=heading)
    if markup:
        d.set_body_use_markup(True)
    d.set_body(body)
    d.add_response("cancel", "Cancel")
    for rid, lbl, app in responses:
        d.add_response(rid, lbl)
        if app == "suggested":
            d.set_response_appearance(rid, Adw.ResponseAppearance.SUGGESTED)
        elif app == "destructive":
            d.set_response_appearance(rid, Adw.ResponseAppearance.DESTRUCTIVE)
    d.set_default_response(default or (responses[0][0] if responses else "cancel"))
    d.set_close_response("cancel")
    if extra is not None:
        d.set_extra_child(extra)
    d.connect("response", lambda _d, rid: p.resolve(None if rid == "cancel" else rid))   # first one wins
    d.present(parent)
    return p


def message(parent, heading: str, body: str, markup: bool = False) -> Pending:
    p = Pending()
    d = Adw.AlertDialog(heading=heading)
    if markup:
        d.set_body_use_markup(True)
    d.set_body(body)
    d.add_response("ok", "OK")
    d.connect("response", lambda *_: p.resolve(None))
    d.present(parent)
    return p


def passphrase_dialog(parent, new: bool, checker=None, title: str | None = None, error: str = "",
                      _p: Pending | None = None) -> Pending:
    """new: ask twice (min. 8 chars). checker(pw) -> Pending[bool] for existing passphrases; a wrong
    passphrase re-opens the dialog with a message. Resolves with the passphrase or None."""
    p = _p or Pending()
    d = Adw.AlertDialog(heading=title or ("Encryption passphrase" if new else "Passphrase"))
    d.set_body("AES-256 via gpg. Without the passphrase the backup cannot be restored - nobody can "
               "recover it. At least 8 characters; 12 or more recommended." if new else
               "Enter the passphrase.")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    e1 = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="passphrase", activates_default=True)
    e2 = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="repeat passphrase", activates_default=True)
    msg = label(error, wrap=True, css=("error",))
    box.append(e1)
    if new:
        box.append(e2)
    box.append(msg)
    d.set_extra_child(box)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", "OK")
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    d.set_response_enabled("ok", False)

    def changed(*_):
        a, b = e1.get_text(), e2.get_text()
        ok = bool(a) and (not new or (len(a) >= 8 and a == b))
        if new and a and len(a) < 8:
            msg.set_text("At least 8 characters.")
        elif new and b and a != b:
            msg.set_text("The passphrases do not match.")
        else:
            msg.set_text("")
        d.set_response_enabled("ok", ok)
    e1.connect("changed", changed)
    e2.connect("changed", changed)

    answered = []

    def response(_d, rid):
        if answered:              # only the first response counts (closing may emit another)
            return
        answered.append(rid)
        if rid != "ok":
            p.resolve(None)
            return
        pw = e1.get_text()
        if checker is None:
            p.resolve(pw)
            return

        def checked(ok, good):
            if ok and good:
                p.resolve(pw)
            else:
                passphrase_dialog(parent, new, checker, title,
                                  "Wrong passphrase (or the backup is damaged)." if ok else str(good), p)
        checker(pw).then(lambda ok, v: GLib.idle_add(lambda: (checked(ok, v), False)[1]))
    d.connect("response", response)
    d.present(parent)
    e1.grab_focus()
    return p


class NetworkDialog(Adw.Dialog):
    """Connect / disconnect SMB and NFS shares through the helper. Resolves with a mount path."""

    def __init__(self, win: "MainWindow", prefill: dict | None = None):
        super().__init__(title="Network shares (NAS)", content_width=720, content_height=640)
        self.win = win
        self.result = Pending()
        self.rows: list[dict] = []
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        self.list_group = Adw.PreferencesGroup(title="Saved and connected shares")
        page.add(self.list_group)
        form = Adw.PreferencesGroup(title="Connect")
        self.proto = Adw.ComboRow(title="Protocol", model=Gtk.StringList.new(["SMB / Windows share", "NFS"]))
        self.server = Adw.EntryRow(title="Server name or IP")
        self.share = Adw.EntryRow(title="Share (SMB) or /export/path (NFS)")
        self.user = Adw.EntryRow(title="User (empty = guest)")
        self.pw = Adw.PasswordEntryRow(title="Password")
        self.domain = Adw.EntryRow(title="Domain (optional)")
        self.vers_ids = [v for _, v in E.SMB_VERSIONS]
        self.vers = Adw.ComboRow(title="SMB version", model=Gtk.StringList.new([n for n, _ in E.SMB_VERSIONS]))
        self.save = Adw.SwitchRow(title="Save connection", active=True)
        self.savepw = Adw.SwitchRow(title="Remember password", subtitle="stored in plain text, readable by root only")
        for w in (self.proto, self.server, self.share, self.user, self.pw, self.domain, self.vers,
                  self.save, self.savepw):
            form.add(w)
        page.add(form)
        self.msg = label("", wrap=True, selectable=True)
        self.msg.set_margin_start(12)
        self.msg.set_margin_end(12)
        btn = Gtk.Button(label="Connect", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        btn.connect("clicked", self.connect_clicked)
        self.connect_btn = btn
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_bottom=18)
        box.append(page)
        box.append(self.msg)
        box.append(btn)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(box)
        tv.set_content(sw)
        self.set_child(tv)
        self.connect("closed", lambda *_: self.result.resolve(None))
        self.prefilled_pw = False
        if prefill:
            self.fill(prefill)
            self.say("Enter the password (leave the user empty for guest access) and press Connect.")
        self.refresh()

    def say(self, text: str, error: bool = False) -> None:
        self.msg.set_markup(f'<span foreground="{COLORS["error"]}">{GLib.markup_escape_text(text)}</span>'
                            if error else GLib.markup_escape_text(text))

    def fill(self, c: dict) -> None:
        self.proto.set_selected(1 if c.get("proto") == "nfs" else 0)
        self.server.set_text(c.get("server", ""))
        self.share.set_text(c.get("share", ""))
        self.user.set_text(c.get("user", ""))
        self.domain.set_text(c.get("domain", ""))
        self.pw.set_text(c.get("password", ""))
        v = c.get("vers") or "auto"
        self.vers.set_selected(self.vers_ids.index(v) if v in self.vers_ids else 0)
        self.prefilled_pw = bool(c.get("has_password"))
        self.savepw.set_active(self.prefilled_pw)

    def refresh(self) -> None:
        def got(ok, res):
            if not ok:
                self.say(str(res), True)
                return
            for r in self.rows:
                self.list_group.remove(r["_row"])
            self.rows = []
            for r in res["rows"]:
                c = r.get("conn")
                title = (f"{c['proto']}://{c['server']}/{c['share'].strip('/')}" if c else r.get("source", "?"))
                sub = ((c.get("user") or "guest") if c else "connected outside diskimg") \
                    + (f"  -  connected at {r['mounted']}" if r.get("mounted") else "")
                row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(sub))
                if c:
                    use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
                    use.connect("clicked", lambda _b, rr=r: (self.fill({**rr["conn"], "has_password": rr["has_password"]}),
                                                            self.say("Press Connect" + (" (saved password is used)."
                                                                     if rr["has_password"] else "."))))
                    row.add_suffix(use)
                if r.get("mounted"):
                    dis = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
                    dis.connect("clicked", lambda _b, mp=r["mounted"]: self.disconnect(mp))
                    row.add_suffix(dis)
                elif c:
                    fg = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Forget")
                    fg.add_css_class("flat")
                    fg.connect("clicked", lambda _b, k=r["key"]: self.forget(k))
                    row.add_suffix(fg)
                r["_row"] = row
                self.rows.append(r)
                self.list_group.add(row)
            if not self.rows:
                row = Adw.ActionRow(title="No saved shares yet")
                r = {"_row": row}
                self.rows.append(r)
                self.list_group.add(row)
        self.win.helper.call("net_list").then(lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))

    def connect_clicked(self, _b) -> None:
        conn = {"proto": "nfs" if self.proto.get_selected() == 1 else "smb",
                "server": self.server.get_text().strip(), "share": self.share.get_text().strip(),
                "user": self.user.get_text().strip(), "domain": self.domain.get_text().strip(),
                "vers": self.vers_ids[self.vers.get_selected()]}
        pw = self.pw.get_text()
        self.say(f"Connecting to {conn['server']} ...")
        self.connect_btn.set_sensitive(False)

        def done(ok, res):
            self.connect_btn.set_sensitive(True)
            if not ok:
                self.say(str(res), True)
                return
            self.win.toast(f"Connected: {res}")
            self.result.resolve(res)
            self.close()
        self.win.helper.call("net_connect", conn=conn, password=pw, save=self.save.get_active(),
                             savepw=self.savepw.get_active(),
                             use_saved_password=self.prefilled_pw and not pw).then(
            lambda ok, v: GLib.idle_add(lambda: (done(ok, v), False)[1]))

    def disconnect(self, mp: str) -> None:
        self.say(f"Disconnecting {mp} ...")
        self.win.helper.call("net_disconnect", mp=mp).then(
            lambda ok, v: GLib.idle_add(lambda: ((self.say(f"Disconnected {mp}.") if ok else self.say(str(v), True)),
                                                 self.refresh(), False)[2]))

    def forget(self, key: str) -> None:
        self.win.helper.call("net_forget", key=key).then(
            lambda ok, v: GLib.idle_add(lambda: (self.refresh(), False)[1]))


# ----------------------------------------------------------------------------
#  Pages
# ----------------------------------------------------------------------------
class SetupPage(Gtk.Box):
    """Mode, options, source, target, summary and Start."""

    def __init__(self, win: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.mode = "d2i"
        self.devs: list[dict] = []
        self.usage: dict[str, str] = {}
        self.src_sel: str | None = None
        self.dst_sel: str | None = None
        self.brw_rows: list[dict] = []
        self.brw_sel: set[str] = set()
        self.brw_src = None
        self.cache_auto = ""
        self.default_name = ""
        self._plan_timer = 0
        self._plan_seq = 0
        self.light_sig = None

        # ---- sidebar: modes + options
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12, margin_end=12,
                       margin_top=12, margin_bottom=12, width_request=290)
        self.modes = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.modes.add_css_class("navigation-sidebar")
        self.mode_rows = {}
        for key, (title, sub) in E.MODES.items():
            row = Adw.ActionRow(title=title.replace("  ", " ").replace("->", "→"), subtitle=sub,
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name(MODE_ICONS[key]))
            row.mode = key
            self.modes.append(row)
            self.mode_rows[key] = row
        self.modes.connect("row-activated", lambda _l, r: self.set_mode(r.mode))
        side.append(self.modes)

        opts = Adw.PreferencesGroup(title="Options")
        self.opt_group = opts
        self.method = Adw.ComboRow(title="Method", model=Gtk.StringList.new(
            ["Raw - every sector", "Filesystem-aware - used data"]))
        self.comp_vals = [v for _, v in E.COMPRESSION]
        self.comp = Adw.ComboRow(title="Compression", model=Gtk.StringList.new([n for n, _ in E.COMPRESSION]))
        self.enc = Adw.SwitchRow(title="Encrypt (AES-256, gpg)")
        self.verify = Adw.SwitchRow(title="Verify after copy", active=True)
        self.precheck = Adw.SwitchRow(title="Check image before restore", active=True)
        self.fallback = Adw.SwitchRow(title="Raw copy if a filesystem fails", active=True)
        self.btrfs = Adw.SwitchRow(title="Use partclone for btrfs")
        self.sparse = Adw.SwitchRow(title="Sparse image file", active=True)
        self.zero = Adw.SwitchRow(title="Zero-fill unreadable areas", active=True)
        self.gpt = Adw.SwitchRow(title="Fix GPT on larger target", active=True)
        self.direct = Adw.SwitchRow(title="Direct disc access", subtitle="ddrescue -d")
        self.keep = Adw.SwitchRow(title="Keep unpacked data")
        self.retry_vals = [0, 1, 3, 5, 10]
        self.retry = Adw.ComboRow(title="Retry passes on bad sectors",
                                  model=Gtk.StringList.new([str(x) for x in self.retry_vals]))
        self.retry.set_selected(1)
        self.scan = Adw.SwitchRow(title="Used space of unmounted partitions",
                                  subtitle="reads the disks in the background")
        self.opt_rows = {"method": self.method, "comp": self.comp, "enc": self.enc, "verify": self.verify,
                         "precheck": self.precheck, "fallback": self.fallback, "btrfs": self.btrfs,
                         "sparse": self.sparse, "zero": self.zero, "gpt": self.gpt, "direct": self.direct,
                         "keep": self.keep, "retry": self.retry, "scan": self.scan}
        for w in self.opt_rows.values():
            opts.add(w)
            w.connect("notify::selected" if isinstance(w, Adw.ComboRow) else "notify::active",
                      self.option_changed)
        sside = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sside.set_child(opts)
        side.append(sside)
        self.append(side)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ---- main area
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, margin_start=14,
                       margin_end=14, margin_top=12, margin_bottom=12)
        self.src_title = label("", markup=True, css=("heading",))
        main.append(self.src_title)
        self.src_devs = DeviceList(lambda p: self.select_dev("src", p))
        main.append(self.src_devs)
        self.src_file = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row = Gtk.Box(spacing=6)
        self.src_path = Gtk.Entry(hexpand=True, placeholder_text="/path/to/backup.img or /path/to/backup.dimg "
                                                                  "(or smb://server/share/...)")
        self.src_path.connect("changed", lambda *_: self.inputs_changed(src=True))
        b1 = Gtk.Button(label="Image file…")
        b1.connect("clicked", lambda *_: self.choose_src(folder=False))
        b2 = Gtk.Button(label="Backup folder…")
        b2.connect("clicked", lambda *_: self.choose_src(folder=True))
        for w in (self.src_path, b1, b2):
            row.append(w)
        self.src_file.append(row)
        self.src_info = label("", markup=True, wrap=True, css=("dim-label",), selectable=True)
        self.src_file.append(self.src_info)
        main.append(self.src_file)

        self.dst_title = label("", markup=True, css=("heading",))
        main.append(self.dst_title)
        self.dst_devs = DeviceList(lambda p: self.select_dev("dst", p))
        main.append(self.dst_devs)
        self.dst_file = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row = Gtk.Box(spacing=6)
        self.dst_path = Gtk.Entry(hexpand=True, placeholder_text="/path/to/backup (or smb://server/share/...)")
        self.dst_path.connect("changed", lambda *_: self.inputs_changed())
        b3 = Gtk.Button(label="Folder…")
        b3.connect("clicked", lambda *_: self.choose_dst())
        row.append(self.dst_path)
        row.append(b3)
        self.dst_file.append(row)
        self.dst_info = label("", markup=True, wrap=True, css=("dim-label",), selectable=True)
        self.dst_file.append(self.dst_info)
        main.append(self.dst_file)

        self.brw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.brw_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.brw_list.add_css_class("boxed-list")
        self.brw_box.append(self.brw_list)
        self.brw_box.append(label("Unpack to (temporary; only used data takes space):", css=("dim-label",)))
        self.cache_path = Gtk.Entry(placeholder_text=str(E.BROWSE_CACHE))
        self.cache_path.connect("changed", lambda *_: self.inputs_changed())
        self.brw_box.append(self.cache_path)
        self.cache_info = label("", markup=True, css=("dim-label",))
        self.brw_box.append(self.cache_info)
        main.append(self.brw_box)

        spacer = Gtk.Box(vexpand=True)
        main.append(spacer)
        bar = Gtk.Box(spacing=12)
        self.summary = label("", markup=True, wrap=True, hexpand=True)
        self.start_btn = Gtk.Button(label="Start", css_classes=["suggested-action", "pill"], sensitive=False)
        self.start_btn.connect("clicked", lambda *_: self.win.start_job())
        bar.append(self.summary)
        bar.append(self.start_btn)
        main.append(bar)
        self.append(main)
        self.modes.select_row(self.mode_rows["d2i"])
        self.set_mode("d2i")

    # ---------------------------------------------------------------- inputs
    def combo_text(self, row: Adw.ComboRow) -> str:
        return row.get_model().get_string(row.get_selected())

    def plan_input(self) -> dict:
        return {
            "mode": self.mode,
            "method": "fs" if self.method.get_selected() == 1 else "raw",
            "compression": self.comp_vals[self.comp.get_selected()],
            "encrypt": self.enc.get_active(), "verify": self.verify.get_active(),
            "precheck": self.precheck.get_active(), "fallback_raw": self.fallback.get_active(),
            "allow_btrfs": self.btrfs.get_active(), "sparse": self.sparse.get_active(),
            "zero_fill": self.zero.get_active(), "fix_gpt": self.gpt.get_active(),
            "idirect": self.direct.get_active(), "retries": self.retry_vals[self.retry.get_selected()],
            "keep_cache": self.keep.get_active(),
            "src_dev": self.src_sel or "", "dst_dev": self.dst_sel or "",
            "src_path": self.src_path.get_text(), "dst_path": self.dst_path.get_text(),
            "browse_ids": sorted(self.brw_sel), "cache_path": self.cache_path.get_text(),
        }

    def local_engine(self) -> str:
        return E.plan_engine(E.PlanInput.from_dict(self.plan_input()))

    def set_mode(self, mode: str) -> None:
        if mode == "fbk":
            self.win.show_files()
            self.modes.select_row(self.mode_rows[self.mode])
            return
        self.mode = mode
        self.modes.select_row(self.mode_rows[mode])
        src_dev = mode in ("d2i", "d2d")
        dst_dev = mode in ("d2d", "i2d")
        self.src_devs.set_visible(src_dev)
        self.src_file.set_visible(not src_dev)
        self.dst_devs.set_visible(dst_dev)
        self.dst_file.set_visible(mode == "d2i")
        self.brw_box.set_visible(mode == "brw")
        self.dst_title.set_visible(mode != "chk")
        self.src_title.set_markup("<b>Source device</b>" if src_dev else "<b>Backup to use</b>")
        self.dst_title.set_markup("<b>Target device</b>" if dst_dev else
                                  "<b>Partitions to open</b>" if mode == "brw" else "<b>Save backup as</b>")
        if mode == "brw":
            self.brw_src = None
            self.fill_brw()
        self.update_default_name()
        self.update_options()
        self.inputs_changed()

    def update_options(self) -> None:
        m = self.mode
        eng = self.local_engine()
        fs = self.method.get_selected() == 1 and m in ("d2i", "d2d")
        show = {"method": m in ("d2i", "d2d"), "comp": m == "d2i", "enc": m == "d2i",
                "verify": m not in ("chk", "brw"), "precheck": m == "i2d" and eng == "container",
                "keep": m == "brw", "fallback": fs, "btrfs": fs,
                "sparse": m == "d2i" and eng == "ddrescue", "zero": eng == "ddrescue",
                "gpt": m in ("d2d", "i2d") and not fs, "direct": m in ("d2i", "d2d") and eng == "ddrescue",
                "retry": eng == "ddrescue", "scan": True}
        for k, w in self.opt_rows.items():
            w.set_visible(show[k])

    def option_changed(self, w, _pspec) -> None:
        if w is self.scan:
            self.win.request_usage()
            return
        if w in (self.method, self.comp, self.enc):
            self.update_default_name()
        self.update_options()
        self.inputs_changed()

    def inputs_changed(self, src: bool = False) -> None:
        if src:
            self.update_options()
            if self.mode == "brw":
                self.fill_brw()
        if self._plan_timer:
            GLib.source_remove(self._plan_timer)
        self._plan_timer = GLib.timeout_add(250, self._replan)

    def _replan(self) -> bool:
        self._plan_timer = 0
        self._plan_seq += 1
        seq = self._plan_seq
        h = self.win.helper
        if not h.alive:
            return False
        pi = self.plan_input()
        if self.mode in ("i2d", "chk", "brw") and pi["src_path"].strip():
            h.call("path_info", path=pi["src_path"], kind="src").then(
                lambda ok, v: GLib.idle_add(lambda: (self.src_info.set_markup(pango(v["text"]) if ok else ""), False)[1]))
        else:
            self.src_info.set_markup("")
        if self.mode == "d2i" and pi["dst_path"].strip() and not E.is_net_url(pi["dst_path"]):
            h.call("path_info", path=pi["dst_path"], kind="dst").then(
                lambda ok, v: GLib.idle_add(lambda: (self.dst_info.set_markup(pango(v["text"]) if ok else ""), False)[1]))
        else:
            self.dst_info.set_markup("")
        if self.mode == "brw":
            need = sum(r["need"] for r in self.brw_rows if r["id"] in self.brw_sel)
            h.call("cache_info", path=pi["cache_path"], need=need).then(
                lambda ok, v: GLib.idle_add(lambda: (self.cache_info.set_markup(pango(v) if ok else ""), False)[1]))
            if self.win.browse is not None:
                self.summary.set_markup(pango(f"[b $accent]A backup is open[/] "
                                              f"({rich_escape(self.win.browse['name'])}). Start returns to it."))
                self.start_btn.set_sensitive(True)
                return False
        if any(E.is_net_url(x) for x in (pi["src_path"] if self.mode in ("i2d", "chk", "brw") else "",
                                          pi["dst_path"] if self.mode == "d2i" else "")):
            self.summary.set_markup(pango("[b $accent]Network path[/]: Start connects to the share first."))
            self.start_btn.set_sensitive(True)
            return False

        def got(ok, res):
            if seq != self._plan_seq:
                return
            if not ok:
                self.summary.set_markup(pango(f"[b $error]{rich_escape(str(res))}[/]"))
                self.start_btn.set_sensitive(False)
                return
            self.opt_group.set_description(res["label"])
            if res["valid"]:
                self.summary.set_markup(pango(res["message"]))
            else:
                self.summary.set_markup(pango(f"[b $warning]{rich_escape(res['message'])}[/]"))
            self.start_btn.set_sensitive(bool(res["valid"]))
        h.call("plan", plan=pi).then(lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))
        return False

    # ---------------------------------------------------------------- devices
    def set_devices(self, devs: list[dict]) -> None:
        self.devs = devs
        paths = {d["path"] for d in devs}
        if self.src_sel not in paths:
            self.src_sel = None
        if self.dst_sel not in paths:
            self.dst_sel = None
        self.light_sig = self.sig(devs)
        self.fill_devs()
        self.update_default_name()
        self.inputs_changed()

    @staticmethod
    def sig(devs) -> str:
        return "|".join(f"{d['path']}:{d['size']}:{d['fstype']}:{','.join(d['mounts'])}" for d in devs)

    def fill_devs(self) -> None:
        self.src_devs.set_devices(self.devs, self.src_sel, self.dst_sel, self.usage)
        self.dst_devs.set_devices(self.devs, self.dst_sel, self.src_sel, self.usage)

    def set_usage(self, path: str, used, total) -> None:
        text = f"{E.human(used)} {used * 100 / max(total or 1, 1):.0f}%" if used is not None else "-"
        self.usage[path] = text
        self.src_devs.set_usage(path, text)
        self.dst_devs.set_usage(path, text)

    def select_dev(self, side: str, path: str) -> None:
        d = next((x for x in self.devs if x["path"] == path), None)
        if not d:
            return
        if d["busy"]:
            self.win.toast(f"{d['path']} is {d['busy']}. Unmount it first.")
            return
        if side == "dst" and d["ro"]:
            self.win.toast(f"{d['path']} is read-only.")
            return
        if side == "src":
            self.src_sel = path
            if self.dst_sel and self._related(path, self.dst_sel):
                self.dst_sel = None
        else:
            self.dst_sel = path
            if self.src_sel and self._related(path, self.src_sel):
                self.src_sel = None
        self.fill_devs()
        self.update_default_name()
        self.inputs_changed()

    def _related(self, a: str, b: str) -> bool:
        by = {d["path"]: d for d in self.devs}
        if a == b:
            return True
        for x, y in ((a, b), (b, a)):
            p = by.get(x, {}).get("parent")
            while p:
                if p == y:
                    return True
                p = by.get(p, {}).get("parent")
        return False

    def update_default_name(self) -> None:
        if self.mode != "d2i" or not self.win.helper.alive:
            return
        pi = self.plan_input()

        def got(ok, new_name):
            if not ok:
                return
            ext = Path(new_name).suffix
            cur = self.dst_path.get_text().strip()
            if not cur:
                self.dst_path.set_text(str(Path("/mnt") / new_name))
            elif self.default_name and Path(cur).name == self.default_name:
                self.dst_path.set_text(str(Path(cur).parent / new_name))
            else:
                for old in (".img", E.CONTAINER_EXT):
                    if cur.endswith(old) and old != ext:
                        self.dst_path.set_text(cur[: -len(old)] + ext)
                        break
            self.default_name = new_name
        self.win.helper.call("default_name", src_dev=self.src_sel or "", plan=pi).then(
            lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))

    # ---------------------------------------------------------------- browse list
    def fill_brw(self) -> None:
        raw = self.src_path.get_text().strip()
        if raw == self.brw_src:
            return
        self.brw_src = raw
        self.brw_rows, self.brw_sel = [], set()
        self.brw_list.remove_all()
        if not raw or not self.win.helper.alive or E.is_net_url(raw):
            return

        def got(ok, res):
            if not ok or self.brw_src != raw:
                return
            self.brw_rows = res["rows"]
            self.brw_sel = set(res["default"])
            if res["cache"] and (not self.cache_path.get_text().strip()
                                 or self.cache_path.get_text().strip() == self.cache_auto):
                self.cache_auto = res["cache"]
                self.cache_path.set_text(res["cache"])
            self.brw_list.remove_all()
            for r in self.brw_rows:
                row = Adw.ActionRow(title=GLib.markup_escape_text(
                    f"{r['id']}   {r['fs']}   {r['label'] or ''}"),
                    subtitle=GLib.markup_escape_text(
                        f"{E.human(r['size'])}"
                        + (f"   unpacks {'<= ' if r['max'] else ''}{E.human(r['need'])}" if r["need"] else "")
                        + (f"   {r['note']}" if r["note"] else "")))
                cb = Gtk.CheckButton(active=r["id"] in self.brw_sel, sensitive=r["ok"], valign=Gtk.Align.CENTER)
                cb.connect("toggled", lambda b, rid=r["id"]: (self.brw_sel.add(rid) if b.get_active()
                                                             else self.brw_sel.discard(rid), self.inputs_changed()))
                row.add_prefix(cb)
                row.set_activatable_widget(cb)
                self.brw_list.append(row)
            if not self.brw_rows:
                self.brw_list.append(Adw.ActionRow(title="Nothing in this backup can be opened here."))
            self.inputs_changed()
        self.win.helper.call("browse_rows", path=raw).then(lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))

    # ---------------------------------------------------------------- choosers
    def _initial_folder(self, text: str) -> Gio.File | None:
        p = Path(text.strip()) if text.strip() else None
        while p is not None and not p.is_dir() and p != p.parent:
            p = p.parent
        return Gio.File.new_for_path(str(p)) if p else None

    def choose_src(self, folder: bool) -> None:
        dlg = Gtk.FileDialog(title="Choose the backup folder (.dimg)" if folder else "Choose a raw image (.img)")
        f0 = self._initial_folder(self.src_path.get_text())
        if f0:
            dlg.set_initial_folder(f0)

        def done(d, res):
            try:
                f = d.select_folder_finish(res) if folder else d.open_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                path = f.get_path()
                if Path(path).name == E.MANIFEST:
                    path = str(Path(path).parent)
                self.src_path.set_text(path)
        if folder:
            dlg.select_folder(self.win, None, done)
        else:
            dlg.open(self.win, None, done)

    def choose_dst(self) -> None:
        dlg = Gtk.FileDialog(title="Choose the folder for the backup")
        f0 = self._initial_folder(self.dst_path.get_text())
        if f0:
            dlg.set_initial_folder(f0)

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                cur = self.dst_path.get_text().strip()
                name = Path(cur).name if cur else (self.default_name or "disk.img")
                if f.get_path().endswith(E.CONTAINER_EXT):
                    self.dst_path.set_text(f.get_path())
                else:
                    self.dst_path.set_text(str(Path(f.get_path()) / name))
        dlg.select_folder(self.win, None, done)


class RunPage(Gtk.Box):
    """Progress of a running job."""

    def __init__(self, win: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14, margin_end=14,
                         margin_top=12, margin_bottom=12)
        self.win = win
        top = Gtk.Box(spacing=18)
        self.info = label("", markup=True, wrap=True, selectable=True, hexpand=True)
        self.info.set_valign(Gtk.Align.START)
        top.append(self.info)
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top.append(self.steps_box)
        self.append(top)

        self.copy_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.copy_title = label("<b>Copy</b>", markup=True)
        self.copy_frame.append(self.copy_title)
        self.copy_bar = Gtk.ProgressBar(show_text=True)
        self.copy_frame.append(self.copy_bar)
        self.phase = label("", css=("dim-label",), ellipsize=True)
        self.copy_frame.append(self.phase)
        self.stats = Gtk.Grid(column_spacing=12, row_spacing=4)
        self.copy_frame.append(self.stats)
        self.append(self.copy_frame)

        self.items_sw = Gtk.ScrolledWindow(min_content_height=90, max_content_height=220,
                                           propagate_natural_height=True)
        self.items = Gtk.Grid(column_spacing=16, row_spacing=3)
        self.items_sw.set_child(self.items)
        self.append(self.items_sw)

        self.verify_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.verify_frame.append(label("<b>Check / verification</b>", markup=True))
        self.verify_bar = Gtk.ProgressBar(show_text=True)
        self.verify_frame.append(self.verify_bar)
        self.verify_info = label("waiting", css=("dim-label",), ellipsize=True)
        self.verify_frame.append(self.verify_info)
        self.append(self.verify_frame)

        self.log = LogView()
        frame = Gtk.Frame()
        frame.set_child(self.log)
        self.append(frame)

        bar = Gtk.Box(spacing=8)
        self.result = label("<b>Running…</b>", markup=True, hexpand=True, wrap=True)
        self.stop_btn = Gtk.Button(label="Stop", css_classes=["destructive-action"])
        self.stop_btn.connect("clicked", lambda *_: self.win.stop_job())
        self.skip_btn = Gtk.Button(label="Skip check", sensitive=False)
        self.skip_btn.connect("clicked", lambda *_: self.win.helper.call("skip"))
        self.log_btn = Gtk.Button(label="Log folder")
        self.log_btn.connect("clicked", lambda *_: self.win.open_path(str(E.LOG_DIR)))
        self.back_btn = Gtk.Button(label="Back", css_classes=["suggested-action"], sensitive=False)
        self.back_btn.connect("clicked", lambda *_: self.win.show_setup())
        for w in (self.result, self.log_btn, self.stop_btn, self.skip_btn, self.back_btn):
            bar.append(w)
        self.append(bar)
        self.step_rows: dict[str, tuple[Gtk.Image, Gtk.Label]] = {}
        self.item_cells: dict[str, dict[str, Gtk.Label]] = {}
        self.running = False

    COLS = ("Item", "Device", "Size", "FS", "Method", "Copy", "Verify")

    def begin(self, st: dict) -> None:
        self.running = True
        self.info.set_markup(pango("\n".join(st["info"])))
        clear_box(self.steps_box)
        self.step_rows = {}
        for key, name, state in st["steps"]:
            row = Gtk.Box(spacing=6)
            icon = Gtk.Image.new_from_icon_name(STEP_ICONS.get(state, "content-loading-symbolic"))
            lb = label(name)
            row.append(icon)
            row.append(lb)
            self.steps_box.append(row)
            self.step_rows[key] = (icon, lb)
            self.set_step(key, state)
        clear_box(self.items)
        self.item_cells = {}
        for c, name in enumerate(self.COLS):
            self.items.attach(label(f"<b>{name}</b>", markup=True, css=("dim-label",)), c, 0, 1, 1)
        self.items_sw.set_visible(st["items_table"])
        files = st["engine"] in ("files", "files-check")
        self.verify_frame.set_visible(st["engine"] not in ("browse", "files", "files-check"))
        self.copy_frame.set_visible(st["mode"] != "chk")
        self.copy_title.set_markup("<b>" + ("Unpack" if st["engine"] == "browse" else
                                            "Backup" if st["engine"] == "files" else
                                            "Check" if files else "Copy") + "</b>")
        self.copy_bar.set_fraction(0)
        self.verify_bar.set_fraction(0)
        self.verify_info.set_text("waiting")
        self.phase.set_text("")
        clear_box(self.stats)
        self.log.clear()
        self.result.set_markup("<b>Running…</b>")
        self.stop_btn.set_sensitive(True)
        self.skip_btn.set_sensitive(False)
        self.back_btn.set_sensitive(False)

    def set_step(self, key: str, state: str) -> None:
        if key not in self.step_rows:
            return
        icon, lb = self.step_rows[key]
        icon.set_from_icon_name(STEP_ICONS.get(state, "content-loading-symbolic"))
        for c in ("error", "warning", "success", "accent", "dim-label"):
            lb.remove_css_class(c)
            icon.remove_css_class(c)
        css = {"fail": "error", "warn": "warning", "done": "success", "run": "accent",
               "pending": "dim-label", "skip": "dim-label"}.get(state)
        if css:
            lb.add_css_class(css)
            icon.add_css_class(css)

    def set_item(self, iid: str, cells: dict) -> None:
        if iid not in self.item_cells:
            r = len(self.item_cells) + 1
            self.item_cells[iid] = {}
            for c, name in enumerate(self.COLS):
                lb = label(cells.get(name, ""), selectable=False, ellipsize=True)
                lb.set_max_width_chars(34)
                self.items.attach(lb, c, r, 1, 1)
                self.item_cells[iid][name] = lb
        else:
            for k, v in cells.items():
                lb = self.item_cells[iid].get(k)
                if lb is not None:
                    lb.set_text(v)
        for k in ("Copy", "Verify"):
            lb = self.item_cells[iid][k]
            t = lb.get_text()
            for c in ("error", "success"):
                lb.remove_css_class(c)
            if t.startswith(("BAD", "FAILED", "MISSING")):
                lb.add_css_class("error")
            elif t.startswith("ok"):
                lb.add_css_class("success")

    def set_bar(self, which: str, pct: float, info: str | None) -> None:
        bar = self.copy_bar if which == "copy" else self.verify_bar
        bar.set_fraction(max(0.0, min(1.0, pct / 100)))
        bar.set_text(f"{pct:.1f} %")
        if which == "verify" and info is not None:
            self.verify_info.set_text(info)

    def set_pairs(self, pairs) -> None:
        clear_box(self.stats)
        for i, (k, v, alert_) in enumerate(pairs):
            r, c = divmod(i, 3)
            self.stats.attach(label(k, css=("dim-label",)), c * 2, r, 1, 1)
            lb = label(v, css=("heading",) + (("error",) if alert_ else ()), ellipsize=True)
            lb.set_hexpand(True)
            self.stats.attach(lb, c * 2 + 1, r, 1, 1)

    def finish(self, result: str, style: str) -> None:
        self.running = False
        self.result.set_markup(pango(f"[b {style}]{rich_escape(result)}[/]"))
        self.stop_btn.set_sensitive(False)
        self.skip_btn.set_sensitive(False)
        self.back_btn.set_sensitive(True)
        self.back_btn.grab_focus()


class BrowsePage(Gtk.Box):
    """A backup (or the versions of a file backup) mounted read-only."""

    def __init__(self, win: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.sess: dict | None = None
        page = Adw.PreferencesPage(vexpand=True)
        self.group = Adw.PreferencesGroup()
        page.add(self.group)
        self.append(page)
        bar = Gtk.Box(spacing=8, margin_start=14, margin_end=14, margin_bottom=12, margin_top=6)
        self.note = label("", wrap=True, hexpand=True, css=("dim-label",))
        open_all = Gtk.Button(label="Open in file manager", css_classes=["suggested-action"])
        open_all.connect("clicked", lambda *_: self.sess and self.win.open_path(self.sess["root"]))
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self.win.show_setup())
        close = Gtk.Button(label="Unmount & close", css_classes=["destructive-action"])
        close.connect("clicked", lambda *_: self.win.close_browse())
        for w in (self.note, open_all, back, close):
            bar.append(w)
        self.append(bar)
        self.rows: list[Gtk.Widget] = []

    def show(self, sess: dict) -> None:
        self.sess = sess
        for r in self.rows:
            self.group.remove(r)
        self.rows = []
        self.group.set_title(GLib.markup_escape_text(sess["name"]))
        self.group.set_description(GLib.markup_escape_text(
            f"Read-only. Folder: {sess['root']}" + (f"   Unpacked data: {sess['cache']}"
                                                    + (" (kept)" if sess["keep"] else " (deleted on close)")
                                                    if sess.get("cache") else "")))
        mounts = sess["mounts"] or [[sess["root"], sess.get("note", "")]]
        for mp, desc in mounts:
            row = Adw.ActionRow(title=GLib.markup_escape_text(Path(mp).name or mp),
                                subtitle=GLib.markup_escape_text(f"{desc}   {mp}"))
            b = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, m=mp: self.win.open_path(m))
            row.add_suffix(b)
            row.set_activatable_widget(b)
            self.group.add(row)
            self.rows.append(row)
        self.note.set_text(sess.get("note") or "Copy files out with your file manager.")


class FilesPage(Gtk.Box):
    """File backup sets (restic)."""

    def __init__(self, win: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.sets: list[dict] = []
        self.ntfs: list[list[str]] = []
        self.keep_ids: list[str] = []
        self.cur: str | None = None
        self.defaults: list[str] = list(E.DEFAULT_EXCLUDES)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                       margin_top=12, margin_bottom=12, width_request=300)
        left.append(label("<b>Backup sets</b>", markup=True))
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda _l, r: self.load_set(r.sid))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.list)
        left.append(sw)
        bb = Gtk.Box(spacing=6)
        nb = Gtk.Button(label="New set")
        nb.connect("clicked", lambda *_: self.new_set())
        db = Gtk.Button(label="Delete", css_classes=["destructive-action"])
        db.connect("clicked", lambda *_: self.delete_set())
        bb.append(nb)
        bb.append(db)
        left.append(bb)
        self.info = label("", markup=True, wrap=True)
        left.append(self.info)
        self.append(left)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        page = Adw.PreferencesPage(vexpand=True)
        g = Adw.PreferencesGroup(title="Settings")
        self.name = Adw.EntryRow(title="Name")
        self.repo = Adw.EntryRow(title="Repository folder (local disk or NAS share)")
        rb = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Choose folder")
        rb.add_css_class("flat")
        rb.connect("clicked", lambda *_: self.choose_folder(lambda p: self.repo.set_text(p)))
        nas = Gtk.Button(label="NAS…", valign=Gtk.Align.CENTER)
        nas.connect("clicked", lambda *_: self.win.network_dialog().then(
            lambda ok, mp: GLib.idle_add(lambda: (mp and self.repo.set_text(mp), False)[1])))
        self.repo.add_suffix(rb)
        self.repo.add_suffix(nas)
        g.add(self.name)
        g.add(self.repo)
        page.add(g)

        def text_group(title, desc, add_btn=False):
            grp = Adw.PreferencesGroup(title=title, description=desc)
            tv = Gtk.TextView(monospace=True, top_margin=6, bottom_margin=6, left_margin=8, right_margin=8)
            fr = Gtk.Frame()
            s = Gtk.ScrolledWindow(min_content_height=90)
            s.set_child(tv)
            fr.set_child(s)
            grp.add(fr)
            if add_btn:
                b = Gtk.Button(label="Add folder…", halign=Gtk.Align.START, margin_top=6)
                b.connect("clicked", lambda *_: self.choose_folder(
                    lambda p: tv.get_buffer().insert(tv.get_buffer().get_end_iter(),
                                                     ("" if not self.tv_text(tv) else "\n") + p)))
                grp.add(b)
            page.add(grp)
            return tv
        self.linux = text_group("Linux folders", "One per line.", add_btn=True)
        wg = Adw.PreferencesGroup(title="Windows partition", description="Mounted read-only during the backup.")
        self.win_row = Adw.ComboRow(title="Partition", model=Gtk.StringList.new(["(none)"]))
        wg.add(self.win_row)
        page.add(wg)
        self.win_ids: list[str] = [""]
        self.winpaths = text_group("Windows folders", "Relative to the partition, one per line "
                                   "(e.g. /Users/Name/Documents).")
        self.excl = text_group("Exclude patterns", "One per line.")
        og = Adw.PreferencesGroup(title="Versions and checks")
        self.keep_row = Adw.ComboRow(title="Old versions (deleted after a successful backup)",
                                     model=Gtk.StringList.new(["Keep all versions"]))
        self.readall = Adw.SwitchRow(title="Check: read all data", subtitle="slow, most thorough")
        og.add(self.keep_row)
        og.add(self.readall)
        page.add(og)
        right.append(page)
        bar = Gtk.Box(spacing=8, margin_start=14, margin_end=14, margin_bottom=12, margin_top=6)
        self.msg = label("", markup=True, wrap=True, hexpand=True)
        bar.append(self.msg)
        for text, act, css in (("Save", "save", ""), ("Check", "check", ""),
                               ("Browse versions", "browse", ""), ("Run backup", "run", "suggested-action"),
                               ("Back", "back", "")):
            b = Gtk.Button(label=text)
            if css:
                b.add_css_class(css)
            b.connect("clicked", lambda _b, a=act: self.action(a))
            bar.append(b)
        right.append(bar)
        self.append(right)

    @staticmethod
    def tv_text(tv: Gtk.TextView) -> str:
        b = tv.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    @staticmethod
    def tv_lines(tv: Gtk.TextView) -> list[str]:
        return [x.strip() for x in FilesPage.tv_text(tv).splitlines() if x.strip()]

    def say(self, text: str, error: bool = False) -> None:
        self.msg.set_markup(pango(f"[b $error]{rich_escape(text)}[/]" if error else rich_escape(text)))

    def reload(self, select: str | None = None) -> None:
        def got(ok, res):
            if not ok:
                self.say(str(res), True)
                return
            self.sets = res["sets"]
            self.ntfs = res["ntfs"]
            self.defaults = res["defaults"]
            self.keep_ids = [k for k, _ in res["keep"]]
            self.keep_row.set_model(Gtk.StringList.new([n for _, n in res["keep"]]))
            if not res["restic"]:
                self.say("restic is not installed. Re-run the installer to add it.", True)
            self.list.remove_all()
            for s in self.sets:
                row = Adw.ActionRow(title=GLib.markup_escape_text(s.get("name", "?")),
                                    subtitle=GLib.markup_escape_text(s.get("repo", "")), activatable=True)
                row.sid = s["id"]
                self.list.append(row)
            target = select or self.cur or (self.sets[0]["id"] if self.sets else None)
            if target and any(s["id"] == target for s in self.sets):
                self.load_set(target)
            else:
                self.new_set()
        self.win.helper.call("filesets").then(lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))

    def set_win_options(self, keep: str) -> None:
        labels = ["(none)"] + [a for a, _ in self.ntfs]
        self.win_ids = [""] + [b for _, b in self.ntfs]
        if keep and keep not in self.win_ids:
            labels.append(f"(not connected) {keep}")
            self.win_ids.append(keep)
        self.win_row.set_model(Gtk.StringList.new(labels))
        self.win_row.set_selected(self.win_ids.index(keep) if keep in self.win_ids else 0)

    def new_set(self) -> None:
        self.cur = None
        self.list.unselect_all()
        self.name.set_text("")
        self.repo.set_text("")
        self.linux.get_buffer().set_text("")
        self.set_win_options("")
        self.winpaths.get_buffer().set_text("")
        self.excl.get_buffer().set_text("\n".join(self.defaults))
        self.keep_row.set_selected(0)
        self.info.set_markup(pango("[dim]New set - fill in the settings and Save.[/]"))

    def load_set(self, sid: str) -> None:
        s = next((x for x in self.sets if x["id"] == sid), None)
        if not s:
            return
        self.cur = sid
        self.name.set_text(s.get("name", ""))
        self.repo.set_text(s.get("repo", ""))
        self.linux.get_buffer().set_text("\n".join(s.get("linux", [])))
        self.set_win_options(s.get("win_uuid", ""))
        self.winpaths.get_buffer().set_text("\n".join(s.get("win_paths", [])))
        self.excl.get_buffer().set_text("\n".join(s.get("excludes", [])))
        k = s.get("keep", "")
        self.keep_row.set_selected(self.keep_ids.index(k) if k in self.keep_ids else 0)
        self.info.set_markup(pango("[dim]reading repository …[/]"))
        self.win.helper.call("fileset_info", sid=sid).then(
            lambda ok, v: GLib.idle_add(lambda: (self.cur == sid and self.info.set_markup(pango(v) if ok else ""),
                                                 False)[1]))

    def data(self) -> dict:
        return {"name": self.name.get_text(), "repo": self.repo.get_text(), "linux": self.tv_lines(self.linux),
                "win_uuid": self.win_ids[self.win_row.get_selected()] if self.win_ids else "",
                "win_paths": self.tv_lines(self.winpaths), "excludes": self.tv_lines(self.excl),
                "keep": self.keep_ids[self.keep_row.get_selected()] if self.keep_ids else ""}

    def choose_folder(self, cb) -> None:
        dlg = Gtk.FileDialog(title="Choose a folder")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                cb(f.get_path())
        dlg.select_folder(self.win, None, done)

    def delete_set(self) -> None:
        s = next((x for x in self.sets if x["id"] == self.cur), None)
        if not s:
            return

        def flow():
            c = yield alert(self.win, "Delete set?", f"Remove the set '{s['name']}' from the list?\nThe repository "
                            f"{s['repo']} and its password file stay untouched.", [("del", "Delete", "destructive")])
            if c != "del":
                return
            yield self.win.helper.call("fileset_delete", sid=s["id"])
            self.cur = None
            self.say(f"Set '{s['name']}' removed. The repository and its password file were kept.")
            self.reload()
        run_flow(flow(), lambda e: self.say(e, True))

    def action(self, act: str) -> None:
        if act == "back":
            self.win.show_setup()
            return
        run_flow(self.flow(act), lambda e: self.say(e, True))

    def flow(self, act: str):
        s = yield self.win.helper.call("fileset_save", data=self.data(), old_id=self.cur)
        self.cur = s["id"]
        self.say(f"Saved '{s['name']}'.")
        self.reload(select=s["id"])
        if act == "save":
            return
        pre = yield self.win.helper.call("fileset_precheck", sid=s["id"], action=act)
        if pre.get("error"):
            self.say(pre["error"], True)
            return
        if pre.get("new_password"):
            pw_label = label(f"<span size='large'><tt>{GLib.markup_escape_text(pre['new_password'])}</tt></span>",
                             markup=True, selectable=True, xalign=0.5)
            yield alert(self.win, "Repository password",
                        "A new repository will be created. restic encrypts every repository; diskimg "
                        f"generated this password and stores it in {pre['pwfile']} (readable by root only).\n\n"
                        "Write it down and keep it outside this computer. Without it (for example after "
                        "reinstalling Linux) the file backups cannot be read.",
                        [("ok", "I wrote it down", "suggested")], extra=pw_label)
        elif pre.get("need_password"):
            sid = s["id"]
            pw = yield passphrase_dialog(self.win, new=False, title="Repository password",
                                         checker=lambda x: self.win.helper.call("fileset_password", sid=sid,
                                                                                password=x))
            if not pw:
                return
        if act == "browse":
            if self.win.browse is not None:
                self.say("Another backup is open. Close it first (Browse backup).", True)
                return
            self.say("Opening versions …")
            sess = yield self.win.helper.call("fileset_browse", sid=s["id"])
            self.win.browse = sess
            self.win.show_browse()
            return
        prep = yield self.win.helper.call("fileset_prepare", sid=s["id"], action=act,
                                          read_all=self.readall.get_active())
        st = yield self.win.helper.call("start", token=prep["token"])
        self.win.show_run(st)
        yield self.win.helper.call("run")


# ----------------------------------------------------------------------------
#  Main window
# ----------------------------------------------------------------------------
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="diskimg", default_width=1280, default_height=860)
        self.browse: dict | None = None
        self.job_running = False
        self.closing = False
        self.helper = Helper(self.on_event, self.on_helper_exit)

        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="diskimg", subtitle=f"disk imaging  v{E.VERSION}")
        hb.set_title_widget(self.title)
        self.net_btn = Gtk.Button(label="Network", tooltip_text="Connect SMB / NFS shares (NAS)")
        self.net_btn.connect("clicked", lambda *_: self.network_from_header())
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh devices")
        self.refresh_btn.connect("clicked", lambda *_: self.load_devices(full=True))
        hb.pack_start(self.net_btn)
        hb.pack_start(self.refresh_btn)
        menu = Gio.Menu()
        menu.append("Open log folder", "win.logs")
        menu.append("Use the text interface", "win.tui")
        menu.append("About diskimg", "win.about")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        hb.pack_end(mb)
        for name, cb in (("logs", lambda *_: self.open_path(str(E.LOG_DIR))),
                         ("about", lambda *_: self.about()),
                         ("tui", lambda *_: self.tui_hint())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        tv.add_top_bar(hb)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status = Adw.StatusPage(icon_name="dialog-password-symbolic", title="Administrator rights needed",
                                     description="diskimg reads and writes whole disks. Authorize once to start.")
        sb = Gtk.Button(label="Authorize", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        sb.connect("clicked", lambda *_: self.connect_helper())
        self.status.set_child(sb)
        self.setup = SetupPage(self)
        self.run_page = RunPage(self)
        self.browse_page = BrowsePage(self)
        self.files = FilesPage(self)
        self.stack.add_named(self.status, "auth")
        self.stack.add_named(self.setup, "setup")
        self.stack.add_named(self.run_page, "run")
        self.stack.add_named(self.browse_page, "browse")
        self.stack.add_named(self.files, "files")
        tv.set_content(self.stack)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)
        self.stack.set_visible_child_name("auth")
        self.connect("close-request", self.on_close_request)
        GLib.timeout_add_seconds(3, self.poll)
        GLib.idle_add(lambda: (self.connect_helper(), False)[1])

    # ---------------------------------------------------------------- helper
    def connect_helper(self) -> None:
        if self.helper.alive:
            return
        self.status.set_description("Waiting for authorization …")
        self.helper.start()

        def ready(ok, info):
            if not ok:
                self.status.set_description(f"{info}\nPress Authorize to try again.")
                return
            self.helper.call("hello").then(lambda ok2, h: GLib.idle_add(lambda: (self.hello(ok2, h), False)[1]))
        self.helper.ready.then(lambda ok, v: GLib.idle_add(lambda: (ready(ok, v), False)[1]))

    def hello(self, ok: bool, h) -> None:
        if not ok:
            self.status.set_description(str(h))
            return
        if h.get("missing"):
            self.toast("Missing tools: " + ", ".join(h["missing"]) + ". Re-run the installer.", timeout=10)
        self.stack.set_visible_child_name("setup")
        self.load_devices(full=True)
        self.helper.call("browse_info").then(lambda ok2, s: GLib.idle_add(
            lambda: (setattr(self, "browse", s if ok2 else None), False)[1]))

    def on_helper_exit(self, code: int) -> None:
        if self.closing:
            self.get_application().quit()
            return
        if self.job_running:
            self.run_page.finish("The helper stopped unexpectedly - the job was interrupted.", "$error")
            self.job_running = False
        self.browse = None
        self.stack.set_visible_child_name("auth")

    def on_event(self, ev: dict) -> None:
        name = ev.get("event")
        rp = self.run_page
        if name == "log":
            rp.log.add(ev["markup"])
        elif name == "step":
            rp.set_step(ev["key"], ev["state"])
        elif name == "item":
            rp.set_item(ev["id"], ev["cells"])
        elif name == "bar":
            rp.set_bar(ev["which"], ev["pct"], ev.get("info"))
        elif name == "pairs":
            rp.set_pairs(ev["pairs"])
        elif name == "phase":
            rp.phase.set_text(ev["text"])
        elif name == "button":
            (rp.skip_btn if ev["name"] == "skip" else rp.stop_btn).set_sensitive(bool(ev["enabled"]) and rp.running)
        elif name == "usage":
            self.setup.set_usage(ev["path"], ev.get("used"), ev.get("total"))
        elif name == "done":
            self.job_done(ev)

    # ---------------------------------------------------------------- devices
    def load_devices(self, full: bool = False) -> None:
        if not self.helper.alive:
            return

        def got(ok, devs):
            if not ok:
                self.toast(str(devs))
                return
            self.setup.usage = {}
            self.setup.set_devices(devs)
            self.request_usage()
        self.helper.call("devices", probe_excl=True).then(lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))

    def request_usage(self) -> None:
        if not self.helper.alive:
            return
        if not self.setup.scan.get_active():
            self.helper.call("usage_cancel")
        self.helper.call("usage", scan=self.setup.scan.get_active())

    def poll(self) -> bool:
        if (self.helper.alive and not self.job_running and self.stack.get_visible_child_name() == "setup"
                and not getattr(self, "_polling", False)):
            self._polling = True

            def got(ok, devs):
                self._polling = False
                if ok and SetupPage.sig(devs) != self.setup.light_sig:
                    self.load_devices(full=True)
            self.helper.call("devices", probe_excl=False).then(
                lambda ok, v: GLib.idle_add(lambda: (got(ok, v), False)[1]))
        return True

    # ---------------------------------------------------------------- navigation
    def show_setup(self) -> None:
        if self.job_running:
            self.stack.set_visible_child_name("run")
            return
        self.stack.set_visible_child_name("setup")
        self.load_devices(full=True)
        self.setup.inputs_changed()

    def show_run(self, st: dict) -> None:
        self.job_running = True
        self.run_page.begin(st)
        self.stack.set_visible_child_name("run")

    def show_browse(self) -> None:
        if self.browse:
            self.browse_page.show(self.browse)
            self.stack.set_visible_child_name("browse")

    def show_files(self) -> None:
        self.files.reload()
        self.stack.set_visible_child_name("files")

    def toast(self, text: str, timeout: int = 5) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text))
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def open_path(self, path: str) -> None:
        f = Gio.File.new_for_path(path)
        Gtk.FileLauncher.new(f).launch(self, None, None)

    def about(self) -> None:
        a = Adw.AboutDialog(application_name="diskimg", version=E.VERSION, application_icon=APP_ID,
                            developer_name="Bot320 and contributors", license_type=Gtk.License.GPL_3_0_ONLY,
                            copyright="© 2026 Bot320 and contributors",
                            comments="Disk imaging for Arch Linux: raw and filesystem-aware images, "
                                     "compression, encryption, network shares, instant browsing and "
                                     "file backups.",
                            website="https://github.com/Bot320/diskimg")
        a.present(self)

    def tui_hint(self) -> None:
        message(self, "Text interface", "Run  sudo diskimg --tui  in a terminal. It has the same functions "
                "and also works without a desktop (for example on the Arch live ISO).")

    # ---------------------------------------------------------------- network
    def network_dialog(self, prefill: dict | None = None) -> Pending:
        d = NetworkDialog(self, prefill)
        d.present(self)
        return d.result

    def network_from_header(self) -> None:
        def got(ok, mp):
            if ok and mp:
                GLib.idle_add(lambda: (self.after_net_connect(mp), False)[1])
        self.network_dialog().then(got)

    def after_net_connect(self, mp: str) -> None:
        s = self.setup
        if s.mode == "d2i":
            cur = s.dst_path.get_text().strip()
            s.dst_path.set_text(str(Path(mp) / (Path(cur).name if cur else (s.default_name or "disk.img"))))
        else:
            self.toast(f"The share is at {mp}. Choose the backup from there.")

    # ---------------------------------------------------------------- jobs
    def start_job(self) -> None:
        run_flow(self.start_flow(), lambda e: self.toast(e, timeout=10))

    def start_flow(self):
        s = self.setup
        if s.mode == "brw" and self.browse is not None:
            self.show_browse()
            return
        for entry in ((s.src_path,) if s.mode in ("i2d", "chk", "brw") else ()) + \
                     ((s.dst_path,) if s.mode == "d2i" else ()):
            if E.is_net_url(entry.get_text()):
                parsed = yield self.helper.call("net_parse", url=entry.get_text())
                mp = yield self.network_dialog(parsed["conn"])
                if not mp:
                    return
                entry.set_text(str(Path(mp) / parsed["sub"]) if parsed["sub"] else mp)
        s.start_btn.set_sensitive(False)
        self.toast("Preparing …", timeout=2)
        try:
            prep = yield self.helper.call("prepare", plan=s.plan_input())
        finally:
            s.start_btn.set_sensitive(True)
        for w in prep["warnings"]:
            self.toast(w, timeout=8)
        token = prep["token"]
        choice = None
        if prep["resume"]:
            choice = yield alert(self, "Resume previous job?", prep["resume"],
                                 [("resume", "Resume", "suggested"), ("fresh", "Start over", "destructive")])
            if choice is None:
                return
        pw = None
        if prep["passphrase"] == "new":
            pw = yield passphrase_dialog(self, new=True)
            if pw is None:
                return
        elif prep["passphrase"] == "existing":
            pw = yield passphrase_dialog(self, new=False, checker=lambda x: self.helper.call(
                "check_passphrase", token=token, passphrase=x))
            if pw is None:
                return
        info = yield self.helper.call("confirm_info", token=token, resume=choice, passphrase=pw)
        typed = ""
        if not prep["browse"]:
            typed = yield self.confirm_dialog(info)
            if typed is None:
                return
        st = yield self.helper.call("start", token=token, confirm_text=typed)
        self.show_run(st)
        yield self.helper.call("run")

    def confirm_dialog(self, info: dict) -> Pending:
        p = Pending()
        body = pango("\n".join(info["lines"]))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw = Gtk.ScrolledWindow(min_content_height=260, max_content_height=460, propagate_natural_height=True,
                                min_content_width=640)
        lb = label(body, markup=True, wrap=True, selectable=True)
        lb.add_css_class("monospace")
        sw.set_child(lb)
        box.append(sw)
        entry = None
        d = Adw.AlertDialog(heading="Confirm job")
        d.set_prefer_wide_layout(True)
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Start")
        d.set_close_response("cancel")
        if info["danger"]:
            entry = Gtk.Entry(placeholder_text=info["token_text"], activates_default=True)
            box.append(entry)
            d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_response_enabled("go", False)
            entry.connect("changed", lambda e: d.set_response_enabled("go", e.get_text().strip() == info["token_text"]))
        else:
            d.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response("go")
        d.set_extra_child(box)
        d.connect("response", lambda _d, rid: p.resolve((entry.get_text().strip() if entry else "")
                                                        if rid == "go" else None))
        d.present(self)
        if entry:
            entry.grab_focus()
        return p

    def stop_job(self) -> None:
        def flow():
            text = yield self.helper.call("stop_text")
            if not text:
                return
            c = yield alert(self, "Stop?", text, [("stop", "Stop", "destructive")])
            if c == "stop":
                yield self.helper.call("stop")
        run_flow(flow(), lambda e: self.toast(e))

    def job_done(self, ev: dict) -> None:
        self.job_running = False
        self.run_page.finish(ev["result"], ev["style"])
        if ev.get("browse"):
            self.browse = ev["browse"]
        n = Gio.Notification.new("diskimg: " + ("finished" if ev["style"] == "$success" else "needs attention"))
        n.set_body(E.strip_markup(ev["result"]))
        self.get_application().send_notification("job", n)
        if self.closing:
            self.helper.close()
            return
        if ev.get("browse") and ev["style"] == "$success":
            self.show_browse()

    def close_browse(self) -> None:
        def flow():
            errs = yield self.helper.call("browse_close", force=False)
            if errs:
                c = yield alert(self, "Still in use",
                                "A program (for example a file manager window or a terminal) is still using "
                                "the backup:\n" + "\n".join(errs) + "\n\nClose it and try again, or detach "
                                "anyway (it disappears as soon as the program lets go).",
                                [("force", "Detach anyway", "destructive")])
                if c != "force":
                    return
                errs = yield self.helper.call("browse_close", force=True)
                if errs:
                    self.toast("Still busy: " + "; ".join(errs), timeout=10)
                    return
            self.browse = None
            self.toast("Backup unmounted.")
            self.show_setup()
        run_flow(flow(), lambda e: self.toast(e))

    # ---------------------------------------------------------------- closing
    def on_close_request(self, _w) -> bool:
        if self.closing:
            return True
        if self.job_running:
            def flow():
                c = yield alert(self, "Quit?", "A job is running. It will be stopped (a ddrescue job can be "
                                "resumed later) and diskimg closes when it has stopped.",
                                [("quit", "Stop and quit", "destructive")])
                if c == "quit":
                    self.closing = True
                    self.set_sensitive(False)
                    self.toast("Stopping …", timeout=30)
                    yield self.helper.call("stop")
            run_flow(flow(), lambda e: self.toast(e))
            return True
        if self.helper.alive:
            # the helper unmounts browse sessions / shares it mounted and exits on end of input
            self.closing = True
            self.set_sensitive(False)
            self.helper.close()
            GLib.timeout_add_seconds(60, lambda: (self.get_application().quit(), False)[1])
            return True
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=os.environ.get("DISKIMG_APP_ID") or APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win: MainWindow | None = None

    def do_activate(self) -> None:
        if self.win is None:
            self.win = MainWindow(self)
            self.set_accels_for_action("window.close", ["<Control>q", "<Control>w"])
        self.win.present()


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() == 0:
        print("diskimg: the graphical interface runs as your normal user (it asks for the password "
              "itself). As root, use the text interface: diskimg --tui", file=sys.stderr)
        return 3
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
        print("diskimg: libadwaita 1.6 or newer is needed for the graphical interface.", file=sys.stderr)
        return 3
    if not Gtk.init_check():
        return 3
    app = App()
    return app.run([sys.argv[0]])
