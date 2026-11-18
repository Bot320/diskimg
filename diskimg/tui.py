"""diskimg text user interface (Textual). Runs as root; also the fallback when no GUI is available."""
from __future__ import annotations

import os
import pwd
import shutil
import signal
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TextArea,
    Tree,
)

from .engine import (  # noqa: E501
    BORDER,
    BROWSE_CACHE,
    BROWSE_ROOT,
    BrowseSession,
    COMPRESSION,
    CONSOLE,
    CONTAINER_EXT,
    DEFAULT_EXCLUDES,
    Dev,
    Job,
    JobRunner,
    JobSink,
    KEEP_POLICIES,
    LOG_DIR,
    MANIFEST,
    MAP_DIR,
    MODES,
    PlanInput,
    SEL,
    SMB_VERSIONS,
    STATE_DIR,
    VERSION,
    apply_resume_choice,
    block_usage,
    browse_candidates,
    cancel_usage,
    check_passphrase,
    chown_tree,
    close_browse,
    confirm_lines,
    confirm_token,
    conn_key,
    conn_source,
    container_summary,
    copy_item,
    default_backup_name,
    default_cache_dir,
    desktop_user,
    devs_overlap,
    engine_label,
    existing_ancestor,
    fileset_info,
    fileset_job,
    fileset_precheck,
    fileset_set_password,
    find_mounted,
    fs_usage,
    fstype_of,
    human,
    is_container,
    is_net_url,
    load_devices,
    load_filesets,
    load_shares,
    map_totals,
    mount_share,
    net_mounts,
    ntfs_partitions,
    open_in_file_manager,
    parse_mapfile,
    parse_net_url,
    passphrase_kind,
    places_options,
    plan_engine,
    plan_job,
    plan_summary,
    prepare_job,
    read_manifest,
    resume_question,
    save_filesets,
    save_shares,
    selectable,
    set_c_locale,
    shutdown_all,
    start_restic_mount,
    strip_markup,
    tree_size,
    umount_path,
    unique_dest,
    usage_allowed,
    usage_generation,
    validate_fileset,
    wanted_ext,
)

# ----------------------------------------------------------------------------
#  Widgets
# ----------------------------------------------------------------------------
class FSTree(DirectoryTree):
    HIDDEN = {"/proc", "/sys", "/dev", "/run/archiso/bootmnt", "/run/archiso/airootfs"}
    ICON_NODE = "+ "
    ICON_NODE_EXPANDED = "- "
    ICON_FILE = "  "

    def filter_paths(self, paths):
        return [p for p in paths if str(p) not in self.HIDDEN and not p.name.startswith(".")]


class DevTable(DataTable):
    pass


# ----------------------------------------------------------------------------
#  Modals
# ----------------------------------------------------------------------------
class MessageModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter", "close", "Close")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self.t, self.b = title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.t, classes="dlg-title")
            yield Static(self.b, classes="dlg-body")
            with Horizontal(classes="dlg-buttons"):
                yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed)
    def action_close(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, choices: list[tuple[str, str, str]]):
        super().__init__()
        self.t, self.b, self.choices = title, body, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.t, classes="dlg-title")
            yield Static(self.b, classes="dlg-body")
            with Horizontal(classes="dlg-buttons"):
                for key, label, variant in self.choices:
                    yield Button(label, variant=variant, id=f"c-{key}")
                yield Button("Cancel", id="c-cancel")

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed) -> None:
        key = (ev.button.id or "c-cancel")[2:]
        self.dismiss(None if key == "cancel" else key)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PassphraseModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, new: bool, checker=None):
        super().__init__()
        self.new = new
        self.checker = checker
        self.checking = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Encryption passphrase" if self.new else "Passphrase for this backup",
                        classes="dlg-title")
            if self.new:
                yield Static("AES-256 via gpg. [b]Without the passphrase the backup cannot be "
                             "restored. Nobody can recover it.[/b] At least 8 characters; 12 or more recommended.",
                             classes="dlg-body")
            yield Input(password=True, placeholder="passphrase", id="pw1")
            if self.new:
                yield Input(password=True, placeholder="repeat passphrase", id="pw2")
            yield Static("", id="pw-msg")
            with Horizontal(classes="dlg-buttons"):
                yield Button("OK", variant="primary", id="pw-ok")
                yield Button("Cancel", id="pw-cancel")

    def on_mount(self) -> None:
        self.query_one("#pw1", Input).focus()

    def _msg(self, s: str) -> None:
        self.query_one("#pw-msg", Static).update(f"[b $error]{s}[/]")

    @on(Input.Submitted)
    def submitted(self, ev: Input.Submitted) -> None:
        if self.new and ev.input.id == "pw1":
            self.query_one("#pw2", Input).focus()
        else:
            self.ok()

    @on(Button.Pressed, "#pw-ok")
    def ok(self) -> None:
        p1 = self.query_one("#pw1", Input).value
        if not p1:
            self._msg("Enter a passphrase.")
            return
        if "\n" in p1:
            self._msg("Line breaks are not allowed.")
            return
        if self.new:
            if len(p1) < 8:
                self._msg("At least 8 characters.")
                return
            if p1 != self.query_one("#pw2", Input).value:
                self._msg("The passphrases do not match.")
                return
        elif self.checker:
            if self.checking:
                return
            self.checking = True
            self.query_one("#pw-ok", Button).disabled = True
            self.query_one("#pw-msg", Static).update("checking...")
            self.run_checker(p1)
            return
        self.dismiss(p1)

    @work(thread=True, exclusive=True, group="pwcheck")
    def run_checker(self, p1: str) -> None:
        try:
            good = bool(self.checker(p1))
        except Exception:  # noqa: BLE001
            good = False

        def done():
            self.checking = False
            self.query_one("#pw-ok", Button).disabled = False
            if good:
                self.dismiss(p1)
            else:
                self._msg("Wrong passphrase (or the image file is damaged).")
        self.app.call_from_thread(done)

    @on(Button.Pressed, "#pw-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.token = confirm_token(job)

    def compose(self) -> ComposeResult:
        j = self.job
        lines = confirm_lines(j)
        with Vertical(classes="dialog wide"):
            yield Label("Confirm job", classes="dlg-title")
            with VerticalScroll(classes="dlg-scroll"):
                yield Static("\n".join(lines), classes="dlg-body")
            if j.dst_is_dev:
                yield Input(placeholder=self.token, id="token")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Start", variant="error" if j.dst_is_dev else "success",
                             id="go", disabled=j.dst_is_dev)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.job.dst_is_dev:
            self.query_one("#token", Input).focus()
        else:
            self.query_one("#go", Button).focus()

    @on(Input.Changed, "#token")
    def token_changed(self, ev: Input.Changed) -> None:
        self.query_one("#go", Button).disabled = ev.value.strip() != self.token

    @on(Input.Submitted, "#token")
    def token_submit(self, ev: Input.Submitted) -> None:
        if ev.value.strip() == self.token:
            self.dismiss(True)

    @on(Button.Pressed, "#go")
    def go(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class BrowseTree(DirectoryTree):
    ICON_NODE = "+ "
    ICON_NODE_EXPANDED = "- "
    ICON_FILE = "  "


class DirOnlyTree(DirectoryTree):
    ICON_NODE = "+ "
    ICON_NODE_EXPANDED = "- "
    ICON_FILE = "  "

    def filter_paths(self, paths):
        out = []
        for p in paths:
            try:
                if p.is_dir() and not p.name.startswith(".") and str(p) not in FSTree.HIDDEN:
                    out.append(p)
            except OSError:
                pass
        return out


class NetworkModal(ModalScreen[str | None]):
    """Connect / disconnect SMB and NFS shares. Dismisses with the local path of a connected share."""
    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        self.prefill = prefill or {}
        self.saved = load_shares()
        self.rows: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Network shares (NAS)", classes="dlg-title")
            yield DataTable(id="net-list", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="net-row"):
                yield Select([("SMB / Windows share", "smb"), ("NFS", "nfs")], value="smb",
                             allow_blank=False, id="net-proto")
                yield Input(placeholder="server name or IP", id="net-server")
                yield Input(placeholder="share (SMB) or /export/path (NFS)", id="net-share")
            with Horizontal(classes="net-row"):
                yield Input(placeholder="user (empty = guest)", id="net-user")
                yield Input(placeholder="password", password=True, id="net-pass")
                yield Input(placeholder="domain (optional)", id="net-domain")
                yield Select(SMB_VERSIONS, value="auto", allow_blank=False, id="net-vers")
            with Horizontal(classes="net-row"):
                yield Checkbox("Save connection", True, id="net-save")
                yield Checkbox("Remember password (plain text, root only)", False, id="net-savepw")
            yield Static("", id="net-msg")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Connect", variant="success", id="net-connect")
                yield Button("Disconnect", variant="warning", id="net-disconnect")
                yield Button("Forget", id="net-forget")
                yield Button("Close", id="net-close")

    def on_mount(self) -> None:
        t = self.query_one("#net-list", DataTable)
        for c, w in (("Share", 36), ("User", 14), ("Status", 44)):
            t.add_column(c, key=c, width=w)
        self.fill_list()
        if self.prefill:
            self.fill_form(self.prefill)
            self.msg("Enter the password (leave user empty for guest access) and press Connect.")
            self.query_one("#net-pass" if self.prefill.get("user") else "#net-user", Input).focus()
        else:
            self.query_one("#net-server", Input).focus()

    def msg(self, s: str, error: bool = False) -> None:
        self.query_one("#net-msg", Static).update(f"[b $error]{escape(s)}[/]" if error else escape(s))

    def fill_list(self) -> None:
        t = self.query_one("#net-list", DataTable)
        t.clear()
        self.rows = {}
        mounted = {(m.get("source") or "").lower().rstrip("/"): m.get("target") for m in net_mounts()}
        for c in self.saved:
            k = conn_key(c)
            mp = mounted.pop(conn_source(c).lower().rstrip("/"), None)
            self.rows[k] = dict(c, _mp=mp)
            t.add_row(f"{c['proto']}://{c['server']}/{c['share'].strip('/')}", c.get("user") or "guest",
                      f"connected: {mp}" if mp else "-", key=k)
        for src, mp in mounted.items():
            k = "ext:" + src
            self.rows[k] = {"_ext": True, "_mp": mp}
            t.add_row(src, "-", f"connected: {mp}", key=k)

    def fill_form(self, c: dict) -> None:
        self.query_one("#net-proto", Select).value = c.get("proto", "smb")
        self.query_one("#net-server", Input).value = c.get("server", "")
        self.query_one("#net-share", Input).value = c.get("share", "")
        self.query_one("#net-user", Input).value = c.get("user", "")
        self.query_one("#net-domain", Input).value = c.get("domain", "")
        self.query_one("#net-pass", Input).value = c.get("password", "")
        self.query_one("#net-vers", Select).value = c.get("vers") or "auto"
        if c.get("password"):
            self.query_one("#net-savepw", Checkbox).value = True

    def selected_key(self) -> str | None:
        t = self.query_one("#net-list", DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    @on(DataTable.RowSelected, "#net-list")
    def row_selected(self, ev: DataTable.RowSelected) -> None:
        row = self.rows.get(ev.row_key.value)
        if row and not row.get("_ext"):
            self.fill_form(row)
            self.query_one("#net-pass", Input).focus()

    def form_conn(self) -> dict:
        proto = self.query_one("#net-proto", Select).value
        server = self.query_one("#net-server", Input).value.strip()
        share = self.query_one("#net-share", Input).value.strip().replace("\\", "/")
        if not server:
            raise ValueError("Enter the server name or IP address.")
        if not share:
            raise ValueError("Enter the share name (SMB) or the export path (NFS).")
        if proto == "smb":
            share = share.strip("/")
            if "/" in share:
                raise ValueError("SMB: enter only the share name, without sub folders.")
        elif not share.startswith("/"):
            share = "/" + share
        return {"proto": proto, "server": server, "share": share,
                "user": self.query_one("#net-user", Input).value.strip(),
                "domain": self.query_one("#net-domain", Input).value.strip(),
                "vers": self.query_one("#net-vers", Select).value or "auto"}

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.connect()

    @on(Button.Pressed, "#net-connect")
    def connect(self) -> None:
        try:
            conn = self.form_conn()
        except ValueError as e:
            self.msg(str(e), True)
            return
        self.msg(f"Connecting to {conn_source(conn)} ...")
        self.query_one("#net-connect", Button).disabled = True
        self.do_connect(conn, self.query_one("#net-pass", Input).value,
                        self.query_one("#net-save", Checkbox).value,
                        self.query_one("#net-savepw", Checkbox).value)

    @work(thread=True, exclusive=True, group="net")
    def do_connect(self, conn: dict, pw: str, save: bool, savepw: bool) -> None:
        try:
            already = find_mounted(conn)
            mp = mount_share(conn, pw)
        except (ValueError, OSError) as e:
            err = str(e)

            def fail():
                self.query_one("#net-connect", Button).disabled = False
                self.msg(err, True)
            self.app.call_from_thread(fail)
            return

        def ok():
            if not already:
                self.app.net_mounted.add(mp)
            if save:
                entry = dict(conn)
                if savepw and pw:
                    entry["password"] = pw
                self.saved = [s for s in self.saved if conn_key(s) != conn_key(conn)] + [entry]
                try:
                    save_shares(self.saved)
                except OSError as e:
                    self.app.notify(f"Could not save the connection: {e}", severity="warning")
            self.app.notify(f"Connected: {conn_source(conn)} -> {mp}")
            self.dismiss(mp)
        self.app.call_from_thread(ok)

    @on(Button.Pressed, "#net-disconnect")
    def disconnect(self) -> None:
        k = self.selected_key()
        row = self.rows.get(k or "")
        if not row or not row.get("_mp"):
            self.msg("Select a connected share in the list first.", True)
            return
        self.msg(f"Disconnecting {row['_mp']} ...")
        self.do_disconnect(row["_mp"])

    @work(thread=True, exclusive=True, group="net")
    def do_disconnect(self, mp: str) -> None:
        e = umount_path(mp)

        def fin():
            if e:
                self.msg(f"Could not disconnect: {e} (is a backup job or file manager using it?)", True)
                return
            self.app.net_mounted.discard(mp)
            self.msg(f"Disconnected {mp}.")
            self.fill_list()
        self.app.call_from_thread(fin)

    @on(Button.Pressed, "#net-forget")
    def forget(self) -> None:
        k = self.selected_key()
        if not k or k.startswith("ext:"):
            self.msg("Select a saved share in the list first.", True)
            return
        self.saved = [s for s in self.saved if conn_key(s) != k]
        try:
            save_shares(self.saved)
        except OSError as e:
            self.msg(str(e), True)
            return
        self.msg("Removed from the saved list.")
        self.fill_list()

    @on(Button.Pressed, "#net-close")
    def action_cancel(self) -> None:
        self.dismiss(None)


class CopyModal(ModalScreen[None]):
    """Copy a file or folder out of a mounted backup."""
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, src: Path):
        super().__init__()
        self.src = src
        self.cancel = threading.Event()
        self.busy = False
        du = desktop_user()
        self.home = pwd.getpwnam(du[0]).pw_dir if du else "/root"
        if not os.path.isdir(self.home):
            self.home = "/"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide", id="copy-dlg"):
            yield Label(f"Copy  {escape(str(self.src))}", classes="dlg-title")
            yield Static("Choose the destination folder:", classes="opt-label")
            yield Select(places_options([self.home]), value=self.home, allow_blank=False, id="cp-places")
            yield DirOnlyTree(self.home, id="cp-tree")
            yield Input(value=self.home, id="cp-dest")
            yield Static("measuring...", id="cp-info")
            yield ProgressBar(total=100, show_eta=False, id="cp-bar")
            with Horizontal(classes="dlg-buttons"):
                yield Button("Copy here", variant="success", id="cp-go")
                yield Button("Close", id="cp-close")

    def on_mount(self) -> None:
        self.total = (0, 0)
        self.measure()

    @work(thread=True, group="measure")
    def measure(self) -> None:
        try:
            self.total = tree_size(self.src, self.cancel)
        except OSError:
            self.total = (0, 0)
        b, n = self.total
        self.app.call_from_thread(self.query_one("#cp-info", Static).update,
                                  f"{n:,} file(s), {human(b)}")

    @on(Select.Changed, "#cp-places")
    def place(self, ev: Select.Changed) -> None:
        if ev.value and os.path.isdir(str(ev.value)):
            self.query_one("#cp-tree", DirOnlyTree).path = Path(str(ev.value))
            self.query_one("#cp-dest", Input).value = str(ev.value)

    @on(DirectoryTree.DirectorySelected, "#cp-tree")
    def picked(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#cp-dest", Input).value = str(ev.path)

    @on(Button.Pressed, "#cp-go")
    def go(self) -> None:
        if self.busy:
            return
        dest = Path(self.query_one("#cp-dest", Input).value.strip())
        if not dest.is_absolute() or not dest.is_dir():
            self.query_one("#cp-info", Static).update("[b $error]Choose an existing folder.[/]")
            return
        if str(dest).startswith(str(BROWSE_ROOT)):
            self.query_one("#cp-info", Static).update("[b $error]The backup is read-only. Choose "
                                                      "another folder.[/]")
            return
        self.busy = True
        self.query_one("#cp-go", Button).disabled = True
        self.do_copy(dest)

    @work(thread=True, group="copy")
    def do_copy(self, dest_dir: Path) -> None:
        state = {"bytes": 0, "files": 0}
        errors: list[str] = []
        target = unique_dest(dest_dir, self.src.name or "backup-root")
        total = max(self.total[0], 1)
        done_flag = threading.Event()

        def ticker():
            while not done_flag.wait(0.3):
                self.app.call_from_thread(self._prog, state["bytes"] * 100 / total,
                                          f"copying ... {state['files']:,} file(s), {human(state['bytes'])}")
        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        try:
            copy_item(self.src, target, state, self.cancel, errors)
        except OSError as e:
            errors.append(f"{self.src}: {e.strerror or e}")
        finally:
            done_flag.set()
        du = desktop_user()
        try:
            if du and target.exists() and os.stat(dest_dir).st_uid == du[1]:
                chown_tree(target, du[1], du[2])
        except OSError:
            pass
        if self.cancel.is_set():
            msg = f"[b $warning]Cancelled. Partial copy in {escape(str(target))}[/]"
        elif errors:
            msg = (f"[b $warning]Copied {state['files']:,} file(s), {human(state['bytes'])} to "
                   f"{escape(str(target))}; {len(errors)} problem(s): {escape(errors[0][:150])}[/]")
        else:
            msg = (f"[b $success]Copied {state['files']:,} file(s), {human(state['bytes'])} to "
                   f"{escape(str(target))}[/]")
        if errors and LOG_DIR.is_dir():
            try:
                with open(LOG_DIR / "copy-errors.log", "a") as f:
                    f.write(f"--- {datetime.now()} {self.src} -> {target}\n" + "\n".join(errors) + "\n")
            except OSError:
                pass

        def fin():
            self.busy = False
            self.query_one("#cp-go", Button).disabled = False
            self._prog(100, msg)
        self.app.call_from_thread(fin)

    def _prog(self, pct: float, text: str) -> None:
        self.query_one("#cp-bar", ProgressBar).update(total=100, progress=min(pct, 100))
        self.query_one("#cp-info", Static).update(text)

    @on(Button.Pressed, "#cp-close")
    def action_close(self) -> None:
        if self.busy:
            self.cancel.set()
            self.query_one("#cp-info", Static).update("cancelling ...")
            return
        self.cancel.set()
        self.dismiss(None)


class BrowseScreen(Screen):
    BINDINGS = [
        Binding("o", "open_fm", "Open in file manager"),
        Binding("c", "copy", "Copy to..."),
        Binding("u", "close_session", "Unmount & close"),
        Binding("escape", "back", "Back to menu"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, sess: BrowseSession):
        super().__init__()
        self.sess = sess
        self.selected: Path = sess.root

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="#")
        with Horizontal(id="brw-body"):
            with Vertical(id="brw-left", classes="panel"):
                yield BrowseTree(str(self.sess.root), id="brw-tree")
            with VerticalScroll(id="brw-right", classes="panel"):
                yield Static("", id="brw-details")
        with Horizontal(id="brw-actions"):
            yield Static("", id="brw-msg")
            yield Button("Open in file manager", variant="primary", id="b-open")
            yield Button("Copy to...", variant="success", id="b-copy")
            yield Button("Back to menu", id="b-back")
            yield Button("Unmount & close", variant="error", id="b-close")
        yield Footer()

    def on_resize(self, ev) -> None:
        compact = self.size.height < 40 or self.size.width < 120
        self.set_class(compact, "compact")
        for w in self.query("Button"):
            w.compact = compact

    def on_mount(self) -> None:
        left = self.query_one("#brw-left")
        left.border_title = "Backup contents (read-only)"
        left.border_subtitle = str(self.sess.root)
        self.query_one("#brw-right").border_title = "Details"
        self.query_one("#brw-msg", Static).update(
            f"[b]{escape(self.sess.name)}[/b]  [dim]" + (escape(self.sess.note) if self.sess.note else
                                                        f"{len(self.sess.mounts)} partition(s) mounted")
            + "[/]")
        self.show_details(self.sess.root)
        self.query_one("#brw-tree").focus()

    def show_details(self, p: Path) -> None:
        self.selected = p
        lines = [f"[b]{escape(p.name or str(p))}[/b]", f"[dim]{escape(str(p))}[/]", ""]
        try:
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                try:
                    n = sum(1 for _ in os.scandir(p))
                except OSError:
                    n = -1
                lines.append(f"Type      folder ({n} entries)" if n >= 0 else "Type      folder (unreadable)")
            elif stat.S_ISLNK(st.st_mode):
                lines.append(f"Type      link -> {escape(os.readlink(p))}")
            else:
                lines.append("Type      file")
                lines.append(f"Size      {human(st.st_size)} ({st.st_size:,} B)")
            lines.append(f"Modified  {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}")
            lines.append(f"Mode      {stat.filemode(st.st_mode)}  uid {st.st_uid}")
        except OSError as e:
            lines.append(f"[$error]{escape(e.strerror or str(e))}[/]")
        if p == self.sess.root:
            lines += ["", "[b]Mounted partitions[/b]"]
            for mp, desc in self.sess.mounts:
                lines.append(f"  {escape(Path(mp).name)}  [dim]{escape(desc)}[/]")
            if self.sess.caches:
                lines += ["", f"[dim]Unpacked data: {escape(str(self.sess.caches[0].parent))}"
                          f"{' (kept after closing)' if self.sess.keep else ' (deleted on close)'}[/]"]
        self.query_one("#brw-details", Static).update("\n".join(lines))

    @on(Tree.NodeHighlighted, "#brw-tree")
    def highlighted(self, ev: Tree.NodeHighlighted) -> None:
        data = ev.node.data
        path = getattr(data, "path", None)
        if path is not None:
            self.show_details(Path(path))

    def action_open_fm(self) -> None:
        p = self.selected if self.selected.is_dir() else self.selected.parent
        err = open_in_file_manager(str(p))
        if err:
            self.app.notify(err, severity="warning", timeout=10)
        else:
            self.app.notify(f"Opening {p} in the file manager ...")

    @on(Button.Pressed, "#b-open")
    def b_open(self) -> None:
        self.action_open_fm()

    def action_copy(self) -> None:
        self.app.push_screen(CopyModal(self.selected))

    @on(Button.Pressed, "#b-copy")
    def b_copy(self) -> None:
        self.action_copy()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#b-back")
    def b_back(self) -> None:
        self.action_back()

    def _finish_close(self) -> None:
        self.app.browse_session = None
        self.app.notify("Backup unmounted" + ("" if self.sess.keep else " and unpacked data deleted") + ".")
        self.app.pop_screen()

    def action_close_session(self) -> None:
        errs = close_browse(self.sess)
        if not errs:
            self._finish_close()
            return

        def after(c):
            if c == "force":
                errs2 = close_browse(self.sess, force=True)
                if errs2:
                    self.app.notify("Still busy: " + "; ".join(errs2), severity="error", timeout=10)
                    return
                self._finish_close()
        self.app.push_screen(ChoiceModal(
            "Still in use",
            "A program (for example a Dolphin window or a terminal) is still using the backup:\n"
            + "\n".join(errs) + "\n\nClose it and try again, or detach anyway (it disappears as soon "
            "as the program lets go).", [("force", "Detach anyway", "error")]), after)

    @on(Button.Pressed, "#b-close")
    def b_close(self) -> None:
        self.action_close_session()

    def action_quit_app(self) -> None:
        self.app.exit()


class FileBackupScreen(Screen):
    """Backup sets for individual files/folders (restic)."""
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("f5", "run_backup", "Run backup"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self):
        super().__init__()
        self.sets = load_filesets()
        self.cur: str | None = None
        self.ntfs: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="#")
        with Horizontal(id="fb-body"):
            with Vertical(id="fb-left", classes="panel"):
                yield DataTable(id="fb-list", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="fb-btns"):
                    yield Button("New set", id="fb-new", variant="primary")
                    yield Button("Delete", id="fb-del", variant="error")
                yield Static("", id="fb-info")
            with VerticalScroll(id="fb-form", classes="panel"):
                yield Label("Name", classes="opt-label")
                yield Input(placeholder="e.g. Documents", id="fb-name")
                yield Label("Repository folder (local disk or NAS share; created if missing)",
                            classes="opt-label")
                yield Input(placeholder="/mnt/diskimg-net/nas_backups/files", id="fb-repo")
                with Horizontal(classes="fb-row"):
                    yield Select(places_options(), value="/", allow_blank=False, id="fb-places")
                    yield Button("Network", id="fb-net")
                yield DirOnlyTree("/", id="fb-tree")
                yield Label("Linux folders (one per line)", classes="opt-label")
                yield TextArea("", id="fb-linux")
                yield Label("Windows partition (mounted read-only during the backup)", classes="opt-label")
                yield Select([("(none)", "")], value="", allow_blank=False, id="fb-win")
                yield Label("Windows folders, relative to the partition (one per line, e.g. "
                            "/Users/Name/Documents)", classes="opt-label")
                yield TextArea("", id="fb-winpaths")
                yield Label("Exclude patterns (one per line)", classes="opt-label")
                yield TextArea("\n".join(DEFAULT_EXCLUDES), id="fb-excl")
                yield Label("Old versions (deleted after a successful backup)", classes="opt-label")
                yield Select([(v[0], k) for k, v in KEEP_POLICIES.items()], value="", allow_blank=False,
                             id="fb-keep")
                yield Checkbox("Check: read all data (slow, most thorough)", False, id="fb-readall")
        with Horizontal(id="fb-actions"):
            yield Static("", id="fb-msg")
            yield Button("Save", id="fb-save")
            yield Button("Run backup", variant="success", id="fb-run")
            yield Button("Browse versions", variant="primary", id="fb-browse")
            yield Button("Check", id="fb-check")
            yield Button("Back", id="fb-back")
        yield Footer()

    def on_resize(self, ev) -> None:
        compact = self.size.height < 40 or self.size.width < 120
        self.set_class(compact, "compact")
        for w in self.query("Button, Select, Input"):
            try:
                w.compact = compact
            except Exception:
                pass

    def on_mount(self) -> None:
        self.query_one("#fb-left").border_title = "Backup sets"
        self.query_one("#fb-form").border_title = "Settings"
        t = self.query_one("#fb-list", DataTable)
        for c, w in (("Name", 18), ("Repository", 30)):
            t.add_column(c, key=c, width=w)
        self.load_ntfs()
        self.fill_list()
        if self.sets:
            self.load_set(self.sets[0]["id"])
        else:
            self.new_set()

    def on_screen_resume(self) -> None:
        self.sets = load_filesets()        # a job may have updated a set (repo_initialized)
        self.load_ntfs()
        sel = self.query_one("#fb-places", Select)
        sel.set_options(places_options())
        if self.cur:
            self.show_info(self.cur)

    def msg(self, s: str, error: bool = False) -> None:
        self.query_one("#fb-msg", Static).update(f"[b $error]{escape(s)}[/]" if error else escape(s))

    def load_ntfs(self) -> None:
        self.ntfs = ntfs_partitions()
        self._set_win_options(self.query_one("#fb-win", Select).value or "")

    def _set_win_options(self, keep: str) -> None:
        opts = [("(none)", "")] + self.ntfs
        if keep and keep not in [v for _, v in opts]:
            opts.append((f"(not connected) {keep}", keep))
        sel = self.query_one("#fb-win", Select)
        sel.set_options(opts)
        sel.value = keep

    def fill_list(self) -> None:
        t = self.query_one("#fb-list", DataTable)
        t.clear()
        for s in self.sets:
            t.add_row(s.get("name", "?"), s.get("repo", ""), key=s["id"])

    def find(self, sid: str | None) -> dict | None:
        return next((s for s in self.sets if s["id"] == sid), None)

    def new_set(self) -> None:
        self.cur = None
        self.query_one("#fb-name", Input).value = ""
        self.query_one("#fb-repo", Input).value = ""
        self.query_one("#fb-linux", TextArea).text = ""
        self._set_win_options("")
        self.query_one("#fb-winpaths", TextArea).text = ""
        self.query_one("#fb-excl", TextArea).text = "\n".join(DEFAULT_EXCLUDES)
        self.query_one("#fb-keep", Select).value = ""
        self.query_one("#fb-info", Static).update("[dim]New set - fill in the settings and Save.[/]")
        self.query_one("#fb-name", Input).focus()

    def load_set(self, sid: str) -> None:
        s = self.find(sid)
        if not s:
            return
        self.cur = sid
        self.query_one("#fb-name", Input).value = s.get("name", "")
        self.query_one("#fb-repo", Input).value = s.get("repo", "")
        self.query_one("#fb-linux", TextArea).text = "\n".join(s.get("linux", []))
        self._set_win_options(s.get("win_uuid", ""))
        self.query_one("#fb-winpaths", TextArea).text = "\n".join(s.get("win_paths", []))
        self.query_one("#fb-excl", TextArea).text = "\n".join(s.get("excludes", []))
        self.query_one("#fb-keep", Select).value = s.get("keep", "") if s.get("keep", "") in KEEP_POLICIES else ""
        self.show_info(sid)

    def show_info(self, sid: str) -> None:
        self.query_one("#fb-info", Static).update("[dim]reading repository ...[/]")
        self.info_worker(sid)

    @work(thread=True, exclusive=True, group="fbinfo")
    def info_worker(self, sid: str) -> None:
        s = self.find(sid)
        if not s:
            return
        self.app.call_from_thread(self.query_one("#fb-info", Static).update, fileset_info(s))

    @on(DataTable.RowSelected, "#fb-list")
    def picked(self, ev: DataTable.RowSelected) -> None:
        self.load_set(ev.row_key.value)

    @on(Button.Pressed, "#fb-new")
    def b_new(self) -> None:
        self.new_set()

    @on(Button.Pressed, "#fb-del")
    def b_del(self) -> None:
        s = self.find(self.cur)
        if not s:
            return

        def after(c):
            if c != "del":
                return
            self.sets = [x for x in self.sets if x["id"] != s["id"]]
            save_filesets(self.sets)
            self.fill_list()
            self.new_set()
            self.msg(f"Set '{s['name']}' removed. The repository and its password file were kept.")
        self.app.push_screen(ChoiceModal(
            "Delete set?", f"Remove the set '{s['name']}' from the list?\nThe repository "
            f"{s['repo']} and the password file stay untouched.", [("del", "Delete", "error")]), after)

    @on(Select.Changed, "#fb-places")
    def place(self, ev: Select.Changed) -> None:
        if ev.value and os.path.isdir(str(ev.value)):
            self.query_one("#fb-tree", DirOnlyTree).path = Path(str(ev.value))

    @on(DirectoryTree.DirectorySelected, "#fb-tree")
    def tree_pick(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#fb-repo", Input).value = str(ev.path)

    @on(Button.Pressed, "#fb-net")
    def b_net(self) -> None:
        self.net_flow()

    @work(exclusive=True, group="fbnet")
    async def net_flow(self) -> None:
        mp = await self.app.push_screen_wait(NetworkModal())
        self.query_one("#fb-places", Select).set_options(places_options())
        if mp:
            self.query_one("#fb-tree", DirOnlyTree).path = Path(mp)
            self.query_one("#fb-repo", Input).value = mp

    def collect(self) -> dict:
        lines = lambda wid: [x.strip() for x in self.query_one(wid, TextArea).text.splitlines() if x.strip()]
        return validate_fileset({
            "name": self.query_one("#fb-name", Input).value,
            "repo": self.query_one("#fb-repo", Input).value,
            "linux": lines("#fb-linux"), "win_paths": lines("#fb-winpaths"),
            "win_uuid": self.query_one("#fb-win", Select).value or "",
            "excludes": lines("#fb-excl"), "keep": self.query_one("#fb-keep", Select).value or ""},
            self.find(self.cur), self.ntfs)

    def action_save(self) -> dict | None:
        try:
            s = self.collect()
        except ValueError as e:
            self.msg(str(e), True)
            return None
        self.sets = [x for x in self.sets if x["id"] != s["id"]] + [s]
        try:
            save_filesets(self.sets)
        except OSError as e:
            self.msg(f"Could not save: {e}", True)
            return None
        self.cur = s["id"]
        self.fill_list()
        self.msg(f"Saved '{s['name']}'.")
        return s

    @on(Button.Pressed, "#fb-save")
    def b_save(self) -> None:
        self.action_save()

    def action_run_backup(self) -> None:
        self.flow("run")

    @on(Button.Pressed, "#fb-run")
    def b_run(self) -> None:
        self.flow("run")

    @on(Button.Pressed, "#fb-browse")
    def b_browse(self) -> None:
        self.flow("browse")

    @on(Button.Pressed, "#fb-check")
    def b_check(self) -> None:
        self.flow("check")

    @work(exclusive=True, group="fbflow")
    async def flow(self, action: str) -> None:
        s = self.action_save()
        if not s:
            return
        pre = await trio_free_to_thread(fileset_precheck, s, action)
        if pre.get("error"):
            self.msg(pre["error"], True)
            return
        if pre.get("new_password"):
            await self.app.push_screen_wait(MessageModal(
                "Repository password",
                "A new repository will be created. restic encrypts every repository; diskimg "
                f"generated this password and stores it in {pre['pwfile']} (readable by root only):\n\n"
                f"    [b]{escape(pre['new_password'])}[/b]\n\nWrite it down and keep it outside this "
                "computer. Without it (for example after reinstalling Linux) the file backups cannot "
                "be read."))
        elif pre.get("need_password"):
            pw = await self.app.push_screen_wait(
                PassphraseModal(new=False, checker=lambda x: fileset_set_password(s, x)))
            if not pw:
                return
        if action == "browse":
            if self.app.browse_session is not None:
                self.msg("Another backup is open in the browser. Close it first (Browse backup).", True)
                return
            self.msg("Opening versions ...")
            sess, err = await trio_free_to_thread(start_restic_mount, s)
            if err:
                self.msg(err, True)
                return
            self.app.browse_session = sess
            self.app.push_screen(BrowseScreen(sess))
            return
        self.app.push_screen(RunScreen(fileset_job(s, action, self.query_one("#fb-readall", Checkbox).value)))

    @on(Button.Pressed, "#fb-back")
    def action_back(self) -> None:
        self.app.pop_screen()


async def trio_free_to_thread(fn, *a):
    """Run a blocking function in a thread from an asyncio Textual worker."""
    import asyncio
    return await asyncio.to_thread(fn, *a)


# ----------------------------------------------------------------------------
#  Main screen
# ----------------------------------------------------------------------------
class MainScreen(Screen):
    BINDINGS = [
        Binding("1", "mode('d2i')", "Disk->Image"),
        Binding("2", "mode('d2d')", "Disk->Disk"),
        Binding("3", "mode('i2d')", "Image->Disk"),
        Binding("4", "mode('chk')", "Check"),
        Binding("5", "mode('brw')", "Browse"),
        Binding("6", "mode('fbk')", "Files"),
        Binding("n", "network", "NAS"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "start", "Start"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.mode = "d2i"
        self.devs: dict[str, Dev] = {}
        self.src_sel: str | None = None
        self.dst_sel: str | None = None
        self.dev_sig = ""
        self.default_name = ""
        self._man_cache: tuple[str, float, dict | None] = ("", 0.0, None)
        self.brw_rows: list[dict] = []
        self.brw_sel: set[str] = set()
        self.brw_src = ""
        self.cache_auto = ""

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="#")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                with Vertical(id="modes", classes="panel"):
                    for key, (title, _) in MODES.items():
                        yield Button(title, id=f"mode-{key}", classes="mode-btn")
                    yield Static("", id="mode-help")
                with VerticalScroll(id="options", classes="panel"):
                    yield Label("Method", classes="opt-label", id="lbl-method")
                    yield Select([("Raw - every sector", "raw"),
                                  ("Filesystem-aware - used data", "fs")],
                                 value="raw", allow_blank=False, id="opt-method")
                    yield Label("Compression", classes="opt-label", id="lbl-comp")
                    yield Select(COMPRESSION, value=0, allow_blank=False, id="opt-comp")
                    yield Checkbox("Encrypt (AES-256, gpg)", False, id="opt-enc")
                    yield Checkbox("Verify after copy", True, id="opt-verify")
                    yield Checkbox("Check image before restore", True, id="opt-precheck")
                    yield Checkbox("Raw copy if a filesystem fails", True, id="opt-fallback")
                    yield Checkbox("Use partclone for btrfs", False, id="opt-btrfs")
                    yield Checkbox("Sparse image file", True, id="opt-sparse")
                    yield Checkbox("Zero-fill unreadable areas", True, id="opt-zero")
                    yield Checkbox("Fix GPT on larger target", True, id="opt-gpt")
                    yield Checkbox("Direct disc access (-d)", False, id="opt-direct")
                    yield Checkbox("Keep unpacked data", False, id="opt-keep")
                    yield Checkbox("Show used space of unmounted partitions (reads the disks)", False,
                                   id="opt-scan")
                    yield Label("Retry passes on bad sectors", classes="opt-label", id="lbl-retry")
                    yield Select([("0 (none)", 0), ("1", 1), ("3", 3), ("5", 5), ("10", 10)],
                                 value=1, allow_blank=False, id="opt-retry")
            with Vertical(id="main"):
                with Vertical(id="src-panel", classes="panel"):
                    yield DevTable(id="src-table", cursor_type="row", zebra_stripes=True)
                    with Vertical(id="src-file"):
                        yield Input(placeholder="/path/to/backup.img or /path/to/backup.dimg",
                                    id="src-path")
                        yield Static("", id="src-info", classes="info")
                        yield Select(places_options(), value="/", allow_blank=False, id="src-places",
                                     classes="places")
                        yield FSTree("/", id="src-tree")
                with Vertical(id="dst-panel", classes="panel"):
                    yield DevTable(id="dst-table", cursor_type="row", zebra_stripes=True)
                    with Vertical(id="dst-file"):
                        yield Input(placeholder="/path/to/backup", id="dst-path")
                        yield Static("", id="dst-info", classes="info")
                        yield Select(places_options(), value="/", allow_blank=False, id="dst-places",
                                     classes="places")
                        yield FSTree("/", id="dst-tree")
                    with Vertical(id="brw-box"):
                        yield DataTable(id="brw-table", cursor_type="row", zebra_stripes=True)
                        yield Label("Unpack to (temporary; only used data takes space):",
                                    classes="opt-label")
                        yield Input(placeholder=str(BROWSE_CACHE), id="cache-path")
                        yield Static("", id="cache-info", classes="info")
                with Horizontal(id="actionbar"):
                    yield Static("", id="summary")
                    yield Button("Network", id="btn-net")
                    yield Button("Refresh", id="btn-refresh")
                    yield Button("Start", variant="success", id="btn-start", disabled=True)
        yield Footer()

    def on_resize(self, ev) -> None:
        compact = self.size.height < 40 or self.size.width < 120
        self.set_class(compact, "compact")
        for w in self.query("Button, Select, Input"):
            try:
                w.compact = compact
            except Exception:
                pass

    def on_mount(self) -> None:
        for tid in ("#src-table", "#dst-table"):
            t = self.query_one(tid, DevTable)
            for col in (" ", "Device", "Size", "Type", "Model / Label", "FS", "Used", "Bus", "Status"):
                t.add_column(col, key=col)
        bt = self.query_one("#brw-table", DataTable)
        for col, w in ((" ", 3), ("Item", 8), ("FS", 14), ("Label", 14), ("Size", 10),
                       ("Unpacks", 10), ("Note", 44)):
            bt.add_column(col, key=col, width=w)
        self.query_one("#options").border_title = "Options"
        self.query_one("#modes").border_title = "Mode"
        self.load_devs()
        self.set_mode("d2i")
        self.set_interval(3, self.poll_devices)

    # ---- device handling ----
    def load_devs(self, devs: dict | None = None, light_sig: str | None = None) -> None:
        if devs is None:
            try:
                devs = load_devices()
            except Exception as e:
                devs = {}
                self.app.notify(f"lsblk failed: {e}", severity="error")
        self.devs = devs
        self.dev_sig = self._sig(self.devs)
        if light_sig is not None:
            self.dev_sig_light = light_sig
        if self.src_sel not in self.devs:
            self.src_sel = None
        if self.dst_sel not in self.devs:
            self.dst_sel = None
        self.fill_tables()
        self.start_usage()

    @staticmethod
    def _sig(devs) -> str:
        return "|".join(f"{d.path}:{d.size}:{d.busy}:{d.fstype}" for d in devs.values())

    @staticmethod
    def _light_sig(devs) -> str:
        return "|".join(f"{d.path}:{d.size}:{d.fstype}:{','.join(d.mounts)}" for d in devs.values())

    def poll_devices(self) -> None:
        if self.app.screen is not self or getattr(self, "_polling", False):
            return
        self._polling = True
        self.poll_worker()

    @work(thread=True, group="poll")
    def poll_worker(self) -> None:
        """Runs off the UI thread. Only lsblk; the exclusive-open probe runs only on changes."""
        try:
            try:
                light = self._light_sig(load_devices(probe_excl=False))
            except Exception:
                return
            if light == getattr(self, "dev_sig_light", None):
                return
            try:
                full = load_devices()
            except Exception:
                return

            def apply():
                if self.app.screen is self:
                    self.load_devs(full, light)
                    self.update_summary()
            self.app.call_from_thread(apply)
        finally:
            self._polling = False

    def fill_tables(self) -> None:
        for tid, sel in (("#src-table", self.src_sel), ("#dst-table", self.dst_sel)):
            t = self.query_one(tid, DevTable)
            cursor_key = None
            try:
                if t.row_count:
                    cursor_key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                pass
            t.clear()
            for d in self.devs.values():
                if not selectable(d):
                    continue
                name = ("  " * d.depth + ("`-" if CONSOLE else "└ ") if d.depth else "") + d.name
                if d.busy:
                    status = Text("in use", style="bold red")
                elif d.ro:
                    status = Text("read-only", style="yellow")
                else:
                    status = Text("ok", style="green")
                other = self.dst_sel if tid == "#src-table" else self.src_sel
                if other and other in self.devs and self.overlaps(self.devs[other], d):
                    status = Text("selected " + ("target" if tid == "#src-table" else "source"),
                                  style="magenta")
                t.add_row(
                    Text(SEL, style="bold green") if d.path == sel else " ",
                    Text(name, style="bold" if d.path == sel else ""),
                    human(d.size), d.kind, d.desc, d.fstype or (d.pttype and f"[{d.pttype}]") or "-",
                    self.used_text(d),
                    (d.tran or ("usb/rm" if d.rm else "-")), status,
                    key=d.path,
                )
            if cursor_key:
                try:
                    t.move_cursor(row=t.get_row_index(cursor_key))
                except Exception:
                    pass

    @staticmethod
    def usage_key(d: Dev) -> tuple:
        return (d.path, d.size, d.fstype, d.uuid, tuple(d.mounts))

    def used_text(self, d: Dev):
        if d.fstype == "swap" or d.kind not in ("part", "disk", "loop") or (d.children and not d.fstype):
            return "-"
        v = self.app.usage.get(self.usage_key(d), "pending")
        if v == "pending":
            if not d.mounts and not self.opt("#opt-scan"):
                return Text("?", style="dim")
            return Text("...", style="dim")
        if not v:
            return "-"
        used, total = v
        return f"{human(used)} {used * 100 / max(total, 1):.0f}%"

    def start_usage(self) -> None:
        if self.app.screen is not self:
            return
        todo = [d for d in self.devs.values() if selectable(d) and d.fstype != "swap"
                and not (d.children and not d.fstype) and self.usage_key(d) not in self.app.usage]
        if todo:
            self.usage_worker(todo, usage_generation(), bool(self.opt("#opt-scan")))

    def on_screen_suspend(self) -> None:
        # another screen takes over: stop all disk scans (a job may be about to start)
        cancel_usage()

    @work(thread=True, group="usage")
    def usage_worker(self, todo: list, gen: int, scan: bool) -> None:
        for d in todo:
            if not usage_allowed(gen):
                return
            key = self.usage_key(d)
            inflight = self.app.usage_inflight
            if key in self.app.usage or key in inflight:
                continue
            inflight.add(key)
            try:
                v = fs_usage(d, gen, scan)
            except Exception:
                v = None
            finally:
                inflight.discard(key)
            if not usage_allowed(gen):
                return          # killed or superseded scan: do not cache its result
            if v is None and scan is False and not d.mounts:
                continue        # not scanned: leave it uncached so enabling the option scans it
            self.app.usage[key] = v
            self.app.call_from_thread(self.fill_tables)

    @staticmethod
    def overlaps(a: Dev, b: Dev) -> bool:
        return devs_overlap(a, b)

    # ---- option helpers ----
    def opt(self, wid: str):
        return self.query_one(wid).value

    def plan_input(self) -> PlanInput:
        """The current choices, independent of the widgets."""
        return PlanInput(
            mode=self.mode,
            method=self.opt("#opt-method") or "raw",
            compression=int(self.opt("#opt-comp") or 0),
            encrypt=bool(self.opt("#opt-enc")),
            verify=bool(self.opt("#opt-verify")),
            precheck=bool(self.opt("#opt-precheck")),
            fallback_raw=bool(self.opt("#opt-fallback")),
            allow_btrfs=bool(self.opt("#opt-btrfs")),
            sparse=bool(self.opt("#opt-sparse")),
            zero_fill=bool(self.opt("#opt-zero")),
            fix_gpt=bool(self.opt("#opt-gpt")),
            idirect=bool(self.opt("#opt-direct")),
            retries=int(self.opt("#opt-retry") or 0),
            keep_cache=bool(self.opt("#opt-keep")),
            src_dev=self.src_sel or "",
            dst_dev=self.dst_sel or "",
            src_path=self.query_one("#src-path", Input).value,
            dst_path=self.query_one("#dst-path", Input).value,
            browse_ids=sorted(self.brw_sel),
            cache_path=self.query_one("#cache-path", Input).value,
        )

    def engine(self) -> str:
        return plan_engine(self.plan_input())

    def src_container(self) -> bool:
        raw = self.query_one("#src-path", Input).value.strip()
        return bool(raw) and is_container(Path(raw))

    def read_manifest(self, p: Path) -> dict | None:
        return read_manifest(p)

    # ---- mode handling ----
    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for key in MODES:
            b = self.query_one(f"#mode-{key}", Button)
            b.variant = "primary" if key == mode else "default"
        self.query_one("#mode-help", Static).update(MODES[mode][1])
        src_is_dev = mode in ("d2i", "d2d")
        dst_is_dev = mode in ("d2d", "i2d")
        self.query_one("#src-table").display = src_is_dev
        self.query_one("#src-file").display = not src_is_dev
        self.query_one("#dst-table").display = dst_is_dev
        self.query_one("#dst-file").display = mode == "d2i"
        self.query_one("#brw-box").display = mode == "brw"
        self.query_one("#dst-panel").display = mode != "chk"
        self.query_one("#src-panel").border_title = "Source device" if src_is_dev else "Backup to use"
        self.query_one("#dst-panel").border_title = ("Target device" if dst_is_dev else
                                                     "Partitions to open" if mode == "brw" else
                                                     "Save backup as")
        self.query_one("#src-panel").border_subtitle = (
            "click / Enter selects" if src_is_dev else "pick an .img file or a .dimg folder")
        self.query_one("#dst-panel").border_subtitle = (
            "click / Enter selects" if dst_is_dev else "click / Enter toggles" if mode == "brw" else
            "pick a folder in the tree, edit the name")
        if mode == "brw":
            self.fill_brw()
        self.update_default_name()
        self.update_options()
        self.update_summary()

    def update_options(self) -> None:
        m = self.mode
        eng = self.engine()
        fs = self.opt("#opt-method") == "fs" and m in ("d2i", "d2d")
        show = {
            "#lbl-method": m in ("d2i", "d2d"), "#opt-method": m in ("d2i", "d2d"),
            "#lbl-comp": m == "d2i", "#opt-comp": m == "d2i", "#opt-enc": m == "d2i",
            "#opt-verify": m not in ("chk", "brw"), "#opt-precheck": m == "i2d" and eng == "container",
            "#opt-keep": m == "brw",
            "#opt-fallback": fs, "#opt-btrfs": fs,
            "#opt-sparse": m == "d2i" and eng == "ddrescue",
            "#opt-zero": eng == "ddrescue",
            "#opt-gpt": m in ("d2d", "i2d") and not fs,
            "#opt-direct": m in ("d2i", "d2d") and eng == "ddrescue",
            "#lbl-retry": eng == "ddrescue", "#opt-retry": eng == "ddrescue",
        }
        for wid, vis in show.items():
            self.query_one(wid).display = vis
        self.query_one("#options").border_subtitle = engine_label(
            m, self.opt("#opt-method"), self.opt("#opt-comp"), self.opt("#opt-enc"),
            self.src_container()).split(":")[0]

    def action_mode(self, mode: str) -> None:
        if mode == "fbk":
            self.app.push_screen(FileBackupScreen())
            return
        self.set_mode(mode)

    @on(Button.Pressed, ".mode-btn")
    def mode_btn(self, ev: Button.Pressed) -> None:
        self.action_mode((ev.button.id or "mode-d2i")[5:])

    def action_refresh(self) -> None:
        self.load_devs()
        self.update_summary()
        self.app.notify("Device list refreshed")

    @on(Button.Pressed, "#btn-refresh")
    def refresh_btn(self) -> None:
        self.action_refresh()

    # ---- selections ----
    # ---- network / places ----
    def refresh_places(self) -> None:
        opts = places_options()
        for sid in ("#src-places", "#dst-places"):
            sel = self.query_one(sid, Select)
            cur = sel.value
            sel.set_options(opts)
            if cur in [v for _, v in opts]:
                sel.value = cur

    def action_network(self) -> None:
        self.network_flow()

    @work(exclusive=True, group="net")
    async def network_flow(self) -> None:
        mp = await self.app.push_screen_wait(NetworkModal())
        self.refresh_places()
        if mp:
            self.jump_to(mp)

    def jump_to(self, path: str) -> None:
        self.refresh_places()
        known = [v for _, v in places_options()]
        for sid, tid in (("#src-places", "#src-tree"), ("#dst-places", "#dst-tree")):
            sel = self.query_one(sid, Select)
            if path in known:
                sel.value = path
            self.query_one(tid, FSTree).path = Path(path)

    @on(Button.Pressed, "#btn-net")
    def net_btn(self) -> None:
        self.action_network()

    # ---- browse partition list ----
    def fill_brw(self) -> None:
        raw = self.query_one("#src-path", Input).value.strip()
        t = self.query_one("#brw-table", DataTable)
        if raw != self.brw_src:
            self.brw_src = raw
            self.brw_rows = []
            self.brw_sel = set()
            p = Path(raw) if raw else None
            try:
                if p and is_container(p):
                    man = self.read_manifest(p)
                    self.brw_rows = browse_candidates(p, man) if man else []
                elif p and p.is_file():
                    self.brw_rows = browse_candidates(p, None)
            except Exception:
                self.brw_rows = []
            ok_rows = [r for r in self.brw_rows if r["ok"]]
            if ok_rows:
                self.brw_sel = {max(ok_rows, key=lambda r: r["size"])["id"]}
            need = sum(r["need"] for r in self.brw_rows if r["id"] in self.brw_sel)
            inp = self.query_one("#cache-path", Input)
            if p and (not inp.value.strip() or inp.value.strip() == self.cache_auto):
                try:
                    self.cache_auto = default_cache_dir(p, need)
                    inp.value = self.cache_auto
                except Exception:
                    pass
        t.clear()
        for r in self.brw_rows:
            mark = Text(SEL, style="bold green") if r["id"] in self.brw_sel else " "
            t.add_row(mark, r["id"], r["fs"], r["label"] or "-", human(r["size"]),
                      ("<= " if r["max"] else "") + human(r["need"]) if r["need"] else "-",
                      Text(r["note"], style="yellow" if not r["ok"] else "dim"), key=r["id"])

    def update_cache_info(self) -> None:
        info = self.query_one("#cache-info", Static)
        need = sum(r["need"] for r in self.brw_rows if r["id"] in self.brw_sel)
        raw = self.query_one("#cache-path", Input).value.strip()
        if not need:
            info.update("[dim]nothing needs to be unpacked[/]" if self.brw_rows else "")
            return
        try:
            base = existing_ancestor(Path(raw or "/"))
            free = shutil.disk_usage(base).free
            info.update(f"needs about {human(need)}   free there: {human(free)} ({fstype_of(base)})")
        except OSError:
            info.update("")

    @on(DataTable.RowSelected)
    def row_selected(self, ev: DataTable.RowSelected) -> None:
        if ev.data_table.id == "brw-table":
            rid = ev.row_key.value
            row = next((r for r in self.brw_rows if r["id"] == rid), None)
            if row and not row["ok"]:
                self.app.notify(f"{rid}: {row['note']}", severity="warning")
                return
            if rid in self.brw_sel:
                self.brw_sel.discard(rid)
            else:
                self.brw_sel.add(rid)
            self.fill_brw()
            try:
                ev.data_table.move_cursor(row=ev.data_table.get_row_index(rid))
            except Exception:
                pass
            self.update_summary()
            return
        path = ev.row_key.value
        d = self.devs.get(path)
        if not d:
            return
        is_src = ev.data_table.id == "src-table"
        if d.busy:
            self.app.notify(f"{d.path} is {d.busy}. Unmount it first.", severity="warning")
            return
        if is_src:
            self.src_sel = path
            if self.dst_sel and self.overlaps(d, self.devs[self.dst_sel]):
                self.dst_sel = None
        else:
            if d.ro:
                self.app.notify(f"{d.path} is read-only.", severity="warning")
                return
            self.dst_sel = path
            if self.src_sel and self.overlaps(d, self.devs[self.src_sel]):
                self.src_sel = None
        self.fill_tables()
        self.update_default_name()
        self.update_summary()

    def wanted_ext(self) -> str:
        return wanted_ext(self.plan_input())

    def update_default_name(self) -> None:
        if self.mode != "d2i":
            return
        ext = self.wanted_ext()
        new_name = default_backup_name(self.devs.get(self.src_sel or ""), ext)
        inp = self.query_one("#dst-path", Input)
        cur = inp.value.strip()
        if not cur:
            inp.value = str(Path("/mnt") / new_name) if Path("/mnt").is_dir() else "/" + new_name
        elif self.default_name and Path(cur).name == self.default_name:
            inp.value = str(Path(cur).parent / new_name)
        else:
            for old in (".img", CONTAINER_EXT):
                if cur.endswith(old) and old != ext:
                    inp.value = cur[: -len(old)] + ext
                    break
        self.default_name = new_name

    @on(DirectoryTree.FileSelected, "#src-tree")
    def src_file(self, ev: DirectoryTree.FileSelected) -> None:
        p = ev.path
        self.query_one("#src-path", Input).value = str(p.parent if p.name == MANIFEST else p)

    @on(DirectoryTree.DirectorySelected, "#src-tree")
    def src_dir(self, ev: DirectoryTree.DirectorySelected) -> None:
        if is_container(Path(ev.path)):
            self.query_one("#src-path", Input).value = str(ev.path)

    @on(DirectoryTree.FileSelected, "#dst-tree")
    def dst_file(self, ev: DirectoryTree.FileSelected) -> None:
        self.query_one("#dst-path", Input).value = str(ev.path)

    @on(DirectoryTree.DirectorySelected, "#dst-tree")
    def dst_dir(self, ev: DirectoryTree.DirectorySelected) -> None:
        inp = self.query_one("#dst-path", Input)
        if Path(ev.path).name.endswith(CONTAINER_EXT):
            inp.value = str(ev.path)
            return
        name = Path(inp.value).name if inp.value.strip() else (self.default_name or "disk.img")
        inp.value = str(Path(ev.path) / name)

    @on(Input.Changed)
    def input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id == "src-path":
            self.update_options()
            if self.mode == "brw":
                self.fill_brw()
        self.update_summary()

    @on(Checkbox.Changed)
    def cb_changed(self, ev: Checkbox.Changed) -> None:
        if ev.checkbox.id == "opt-scan":
            if not ev.value:
                cancel_usage()
            self.fill_tables()
            self.start_usage()
            return
        if ev.checkbox.id == "opt-enc":
            self.update_default_name()
        self.update_options()
        self.update_summary()

    @on(Select.Changed)
    def sel_changed(self, ev: Select.Changed) -> None:
        if ev.select.id in ("src-places", "dst-places"):
            if ev.value and os.path.isdir(str(ev.value)):
                tid = "#src-tree" if ev.select.id == "src-places" else "#dst-tree"
                tree = self.query_one(tid, FSTree)
                if str(tree.path) != str(ev.value):
                    tree.path = Path(str(ev.value))
            return
        if ev.select.id in ("opt-method", "opt-comp"):
            self.update_default_name()
        self.update_options()
        self.update_summary()

    # ---- planning / validation ----
    def plan(self) -> Job:
        if self.mode == "brw" and self.brw_src != self.query_one("#src-path", Input).value.strip():
            self.fill_brw()
        return plan_job(self.plan_input(), self.devs)

    def update_info_lines(self) -> None:
        info = self.query_one("#dst-info", Static)
        raw = self.query_one("#dst-path", Input).value.strip()
        try:
            p = Path(raw)
            if raw and p.parent.is_dir():
                free = shutil.disk_usage(p.parent).free
                info.update(f"filesystem: {fstype_of(p.parent)}   free: {human(free)}"
                            + ("   [yellow]exists[/]" if p.exists() else ""))
            else:
                info.update("[dim]folder does not exist[/]" if raw else "")
        except Exception:
            info.update("")
        sinfo = self.query_one("#src-info", Static)
        raw = self.query_one("#src-path", Input).value.strip()
        try:
            p = Path(raw)
            if raw and is_container(p):
                man = self.read_manifest(p)
                sinfo.update(container_summary(man) if man else "[b $error]unreadable manifest[/]")
            elif raw and p.is_file():
                parts = [f"raw image, {human(p.stat().st_size)} ({p.stat().st_size:,} B)"]
                mp = Path(str(p) + ".map")
                if mp.exists():
                    t = map_totals(parse_mapfile(mp))
                    bad = sum(v for k, v in t.items() if k != "+")
                    parts.append("[green]backup complete[/]" if bad == 0 else
                                 f"[yellow]{human(bad)} unreadable at backup time (zeros)[/]")
                if Path(str(p) + ".sha256").exists():
                    parts.append("checksum file present")
                sinfo.update("   ".join(parts))
            else:
                sinfo.update("")
        except Exception:
            sinfo.update("")

    def net_inputs(self) -> list[Input]:
        out = []
        if self.mode in ("i2d", "chk", "brw"):
            out.append(self.query_one("#src-path", Input))
        if self.mode == "d2i":
            out.append(self.query_one("#dst-path", Input))
        return [i for i in out if is_net_url(i.value)]

    def update_summary(self) -> None:
        self.update_info_lines()
        s = self.query_one("#summary", Static)
        b = self.query_one("#btn-start", Button)
        if self.mode == "brw":
            self.update_cache_info()
            if self.app.browse_session is not None:
                s.update(f"[b $accent]A backup is open[/] ({escape(self.app.browse_session.name)}). "
                         "Start returns to it.")
                b.disabled = False
                return
        if self.net_inputs():
            s.update("[b $accent]Network path[/]: Start connects to the share first.")
            b.disabled = False
            return
        try:
            j = self.plan()
        except ValueError as e:
            s.update(f"[b $warning]{escape(str(e))}[/]")
            b.disabled = True
            return
        s.update(plan_summary(j))
        b.disabled = False

    # ---- start flow ----
    def action_start(self) -> None:
        self.start_flow()

    @work(exclusive=True, group="start")
    async def start_flow(self) -> None:
        if self.mode == "brw" and self.app.browse_session is not None:
            self.app.push_screen(BrowseScreen(self.app.browse_session))
            return
        for inp in self.net_inputs():
            try:
                conn, sub = parse_net_url(inp.value)
            except ValueError as e:
                self.app.notify(str(e), severity="error")
                return
            mp = await self.app.push_screen_wait(NetworkModal(prefill=conn))
            if not mp:
                return
            inp.value = str(Path(mp) / sub) if sub else mp
            self.jump_to(mp)
            self.update_summary()
        try:
            job = self.plan()
        except ValueError as e:
            self.app.notify(str(e), severity="error")
            return

        # analyse source / image layout (reads partition tables: not on the UI thread)
        if job.engine == "container" and job.mode in ("d2i", "d2d"):
            self.app.notify("Analysing the source ...", timeout=3)
        devmap = {d.path: d for d in self.devs.values()}

        def used(node):
            d = devmap.get(node)
            v = self.app.usage.get(self.usage_key(d)) if d else None
            return v[0] if v else None
        try:
            for w in await trio_free_to_thread(prepare_job, job, used):
                self.app.notify(w, severity="warning", timeout=8)
        except ValueError as e:
            self.app.notify(str(e), severity="error", timeout=10)
            return

        # resume detection (ddrescue)
        body = resume_question(job)
        choice = None
        if body:
            choice = await self.app.push_screen_wait(ChoiceModal(
                "Resume previous job?", body,
                [("resume", "Resume", "success"), ("fresh", "Start over", "warning")]))
            if choice is None:
                return
        apply_resume_choice(job, choice)

        # passphrase
        kind = passphrase_kind(job)
        if kind == "new":
            pw = await self.app.push_screen_wait(PassphraseModal(new=True))
            if pw is None:
                return
            job.passphrase = pw
        elif kind == "existing":
            pw = await self.app.push_screen_wait(
                PassphraseModal(new=False, checker=lambda x: check_passphrase(job, x)))
            if pw is None:
                return
            job.passphrase = pw

        if job.engine == "browse":
            self.app.push_screen(RunScreen(job))
            return
        ok = await self.app.push_screen_wait(ConfirmModal(job))
        if ok:
            self.app.push_screen(RunScreen(job))

    @on(Button.Pressed, "#btn-start")
    def start_btn(self) -> None:
        self.action_start()

    def action_quit_app(self) -> None:
        self.app.exit()

    def on_screen_resume(self) -> None:
        if self.app.usage_dirty:
            # a job ran: filesystems may have changed. (Closing a dialog keeps the results.)
            self.app.usage.clear()
            self.app.usage_dirty = False
        self.load_devs()
        self.set_timer(1.5, self.start_usage)    # in case the job screen was still unmounting
        self._man_cache = ("", 0.0, None)
        self.brw_src = "\0"
        if self.mode == "brw":
            self.fill_brw()
        self.refresh_places()
        self.update_options()
        self.update_summary()


# ----------------------------------------------------------------------------
#  Run screen
# ----------------------------------------------------------------------------
STEP_MARK = {"pending": ("[ ]", "dim"), "run": ("[>]", "bold $accent"), "done": ("[x]", "$success"),
             "skip": ("[-]", "dim"), "fail": ("[!]", "bold $error"), "warn": ("[~]", "$warning")}

class TuiSink(JobSink):
    """Forwards job events from the job thread to the Textual UI thread."""

    def __init__(self, scr: "RunScreen"):
        self.scr = scr

    def _call(self, fn, *a) -> None:
        try:
            self.scr.app.call_from_thread(fn, *a)
        except Exception:  # noqa: BLE001 - the app is shutting down
            pass

    def log(self, markup: str) -> None:
        self._call(self.scr.log_line, markup)

    def step(self, key: str, state: str) -> None:
        self._call(self.scr.render_steps)

    def item(self, iid: str, cells: dict) -> None:
        self._call(self.scr.item_row, iid, cells)

    def bar(self, which: str, pct: float, info: str | None = None) -> None:
        self._call(self.scr._bar, which, pct, info)

    def pairs(self, pairs: list[tuple[str, str, bool]]) -> None:
        self._call(self.scr.show_pairs, pairs)

    def phase(self, text: str) -> None:
        self._call(self.scr.set_phase, text)

    def button(self, name: str, enabled: bool) -> None:
        self._call(self.scr._set_btn, f"#btn-{name}", not enabled)


class RunScreen(Screen):
    BINDINGS = [
        Binding("x", "stop_copy", "Stop"),
        Binding("v", "skip_verify", "Skip check/verify"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.runner = JobRunner(job, TuiSink(self))
        self.running = True
        self.quit_after = False
        self.items_rows: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="#")
        with Horizontal(id="run-top"):
            yield Static("", id="jobinfo", classes="panel")
            with VerticalScroll(id="steps-box", classes="panel"):
                yield Static("", id="steps")
        with Vertical(id="copy-panel", classes="panel"):
            yield ProgressBar(total=100, show_eta=False, id="copy-bar")
            yield Static("", id="stats")
        yield DataTable(id="items", cursor_type="none", zebra_stripes=True)
        with Vertical(id="verify-panel", classes="panel"):
            yield ProgressBar(total=100, show_eta=False, id="verify-bar")
            yield Static("waiting", id="verify-info")
        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
        with Horizontal(id="run-actions"):
            yield Static("[b]Running...[/b]", id="result")
            yield Button("Stop", variant="error", id="btn-stop")
            yield Button("Skip check", variant="warning", id="btn-skip", disabled=True)
            yield Button("Back to menu", variant="primary", id="btn-back", disabled=True)
        yield Footer()

    def on_resize(self, ev) -> None:
        compact = self.size.height < 40 or self.size.width < 120
        self.set_class(compact, "compact")
        for w in self.query("Button"):
            w.compact = compact

    def on_mount(self) -> None:
        j = self.job
        # no background disk scans while a job screen exists (they could keep partitions open)
        block_usage(True)
        self._usage_blocked = True
        self.app.usage_dirty = True
        self.query_one("#jobinfo").border_title = "Job"
        self.query_one("#steps-box").border_title = "Steps"
        self.query_one("#copy-panel").border_title = "Copy" if j.mode != "chk" else "Progress"
        self.query_one("#verify-panel").border_title = "Check / Verification"
        self.query_one("#log").border_title = "Log"
        self.query_one("#jobinfo", Static).update("\n".join(self.runner.info_lines()))
        self.render_steps()
        t = self.query_one("#items", DataTable)
        if j.engine == "browse":
            self.query_one("#verify-panel").display = False
            self.query_one("#copy-panel").border_title = "Unpack"
        if j.engine in ("files", "files-check"):
            self.query_one("#verify-panel").display = False
            self.query_one("#copy-panel").border_title = "Backup" if j.engine == "files" else "Check"
        if self.runner.has_items_table():
            for col, w in (("Item", 12), ("Device", 16), ("Size", 10), ("FS", 8), ("Method", 16),
                           ("Copy", 30), ("Verify", 10)):
                t.add_column(col, key=col, width=w)
        else:
            t.display = False
        if j.mode == "chk":
            self.query_one("#copy-panel").display = False
        self.run_job()

    # ---- ui helpers (main thread) ----
    def themed(self, msg: str) -> Text:
        tv = self.app.theme_variables
        for var in ("error", "warning", "success", "accent"):
            msg = msg.replace(f"${var}", tv.get(var, "white"))
        try:
            return Text.from_markup(msg)
        except Exception:  # noqa: BLE001
            return Text(strip_markup(msg))

    def log_line(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(self.themed(msg))

    def render_steps(self) -> None:
        out = []
        for k, name in self.runner.step_names.items():
            mark, style = STEP_MARK[self.runner.steps[k]]
            out.append(f"[{style}]{escape(mark)} {name}[/]")
        self.query_one("#steps", Static).update(self.themed("\n".join(out)))

    def _set_btn(self, wid: str, disabled: bool) -> None:
        if not self.running:
            return
        self.query_one(wid, Button).disabled = disabled

    def item_row(self, iid: str, cells: dict[str, str]) -> None:
        t = self.query_one("#items", DataTable)
        if iid not in self.items_rows:
            vals = [cells.get(c, "") for c in ("Item", "Device", "Size", "FS", "Method", "Copy", "Verify")]
            t.add_row(*vals, key=iid)
            self.items_rows.add(iid)
        else:
            for k, v in cells.items():
                try:
                    t.update_cell(iid, k, v)
                except Exception:  # noqa: BLE001
                    pass

    def show_pairs(self, pairs: list[tuple[str, str, bool]]) -> None:
        g = Table.grid(padding=(0, 2), expand=True)
        for _ in range(3):
            g.add_column(style="dim", no_wrap=True)
            g.add_column(style="bold", ratio=1, no_wrap=True)
        row: list = []
        for k, v, alert in pairs:
            row += [k, Text(v, style="bold red") if alert else v]
            if len(row) == 6:
                g.add_row(*row)
                row = []
        if row:
            row += [""] * (6 - len(row))
            g.add_row(*row)
        self.query_one("#stats", Static).update(g)

    def set_phase(self, text: str) -> None:
        self.query_one("#copy-panel").border_subtitle = text

    def _bar(self, which: str, pct: float, info: str | None = None) -> None:
        self.query_one(f"#{which}-bar", ProgressBar).update(total=100, progress=pct)
        if which == "verify" and info is not None:
            self.query_one("#verify-info", Static).update(info)

    # ---- actions ----
    def action_stop_copy(self) -> None:
        if not self.running:
            return
        body = self.runner.stop_text()
        if body is None:
            return

        def after(c):
            if c == "stop":
                self.runner.request_stop()
        self.app.push_screen(ChoiceModal("Stop?", body, [("stop", "Stop", "error")]), after)

    @on(Button.Pressed, "#btn-stop")
    def stop_btn(self) -> None:
        self.action_stop_copy()

    def action_skip_verify(self) -> None:
        self.runner.request_skip()

    @on(Button.Pressed, "#btn-skip")
    def skip_btn(self) -> None:
        self.action_skip_verify()

    def action_back(self) -> None:
        if not self.running:
            self.app.pop_screen()

    @on(Button.Pressed, "#btn-back")
    def back_btn(self) -> None:
        self.action_back()

    def action_quit_app(self) -> None:
        if not self.running:
            self.app.exit()
            return

        def after(c):
            if c == "quit":
                self.stop_and_quit()
        self.app.push_screen(ChoiceModal(
            "Quit?", "A job is running. It will be stopped and diskimg exits.",
            [("quit", "Stop and quit", "error")]), after)

    def stop_and_quit(self) -> None:
        self.quit_after = True
        self.runner.request_quit()

    # ---- worker ----
    @work(thread=True, exclusive=True)
    def run_job(self) -> None:
        result, style = self.runner.run()
        j = self.job

        def done():
            self.running = False
            self.query_one("#result", Static).update(self.themed(f"[b {style}]{escape(result)}[/]"))
            self.query_one("#btn-stop", Button).disabled = True
            self.query_one("#btn-skip", Button).disabled = True
            b = self.query_one("#btn-back", Button)
            b.disabled = False
            b.focus()
            self.app.bell()
            if self.runner.browse_session is not None:
                self.app.browse_session = self.runner.browse_session
            if self.quit_after:
                self.app.exit()
            elif j.engine == "browse" and self.app.browse_session is not None:
                self.app.switch_screen(BrowseScreen(self.app.browse_session))
        try:
            self.app.call_from_thread(done)
        except Exception:  # noqa: BLE001 - app already gone
            pass

    def on_unmount(self) -> None:
        if getattr(self, "_usage_blocked", False):
            self._usage_blocked = False
            block_usage(False)


# ----------------------------------------------------------------------------
#  App
# ----------------------------------------------------------------------------
CSS = """
Screen { background: $background; }
.panel { border: BORDER $primary; border-title-color: $accent; border-title-style: bold;
         border-subtitle-color: $text-muted; padding: 0 1; }
#body { height: 1fr; }
#sidebar { width: 38; }
#modes { height: auto; }
.mode-btn { width: 100%; margin-bottom: 0; }
#mode-help { color: $text-muted; height: auto; margin-top: 1; }
#options { height: 1fr; }
#options Checkbox { border: none; padding: 0; background: transparent; }
#options Select { margin-bottom: 1; }
.opt-label { color: $text-muted; }
#main { width: 1fr; }
#src-panel, #dst-panel { height: 1fr; }
#src-file, #dst-file { height: 1fr; }
.info { height: auto; max-height: 3; color: $text-muted; }
DevTable { height: 1fr; }
FSTree { height: 1fr; border: none; }
#actionbar { height: 3; padding: 0 1; }
#summary { width: 1fr; content-align: left middle; height: 3; }
#actionbar Button { margin-left: 1; }

#run-top { height: 9; }
#jobinfo { width: 2fr; }
#steps-box { width: 1fr; min-width: 34; }
#copy-panel { height: auto; }
#copy-bar, #verify-bar { width: 100%; margin: 1 0 0 0; }
#copy-bar Bar, #verify-bar Bar { width: 1fr; }
#stats { height: auto; margin-top: 1; }
#items { height: auto; max-height: 10; margin: 0 1; }
#verify-panel { height: auto; }
#verify-info { color: $text-muted; }
#log { height: 1fr; min-height: 4; border: BORDER $primary; border-title-color: $accent; }
#run-actions { height: 3; padding: 0 1; }
#result { width: 1fr; content-align: left middle; height: 3; }
#run-actions Button { margin-left: 1; }

ModalScreen { align: center middle; background: $background 60%; }
.dialog { width: 72; max-width: 100%; height: auto; max-height: 100%; border: thick $accent;
          background: $surface; padding: 0 1; }
.dialog.wide { width: 110; max-width: 98%; }
.dlg-title { text-style: bold; color: $accent; margin-bottom: 1; }
.dlg-body { height: auto; margin-bottom: 1; }
.dlg-scroll { height: auto; }
.dlg-buttons { height: auto; align-horizontal: right; }
.dlg-buttons Button { margin-left: 1; }
#pw-msg { height: auto; }
.places { margin: 0; }
#brw-box { height: 1fr; }
#brw-table { height: 1fr; }
#brw-body { height: 1fr; }
#brw-left { width: 2fr; }
#brw-right { width: 1fr; min-width: 36; }
#brw-actions { height: 3; padding: 0 1; }
#brw-msg { width: 1fr; content-align: left middle; height: 3; }
#brw-actions Button { margin-left: 1; }
.net-row { height: auto; }
.net-row Input { width: 1fr; }
.net-row Select { width: 26; }
.net-row Checkbox { border: none; background: transparent; }
#net-list { height: auto; max-height: 8; }
#net-msg { height: auto; }
#cp-tree { height: 12; border: none; }
#cp-info { height: auto; }
#fb-body { height: 1fr; }
#fb-left { width: 1fr; min-width: 40; }
#fb-form { width: 2fr; }
#fb-list { height: 1fr; }
.fb-btns { height: auto; }
.fb-btns Button { margin-right: 1; }
#fb-info { height: auto; margin-top: 1; }
.fb-row { height: auto; }
.fb-row Select { width: 1fr; }
#fb-tree { height: 8; border: none; }
#fb-form TextArea { height: 5; }
#fb-form Checkbox { border: none; background: transparent; }
#fb-actions { height: 3; padding: 0 1; }
#fb-msg { width: 1fr; content-align: left middle; height: 3; }
#fb-actions Button { margin-left: 1; }
FileBackupScreen.compact #fb-actions { height: 1; }
FileBackupScreen.compact #fb-msg { height: 1; }
BrowseScreen.compact #brw-actions { height: 1; }
BrowseScreen.compact #brw-msg { height: 1; }
BrowseScreen.compact #brw-right { min-width: 26; }

/* small terminals (e.g. 80x25 Linux console) */
MainScreen.compact #sidebar { width: 30; }
MainScreen.compact #mode-help { display: none; }
MainScreen.compact .mode-btn { margin-bottom: 0; }
MainScreen.compact #actionbar { height: 2; }
MainScreen.compact #summary { height: 2; }
MainScreen.compact #actionbar Button { margin-left: 1; }
MainScreen.compact #options Select { margin-bottom: 0; }
RunScreen.compact #run-top { height: 5; }
RunScreen.compact #log { min-height: 3; }
RunScreen.compact #copy-panel, RunScreen.compact #verify-panel { padding: 0 1; }
RunScreen.compact #copy-bar, RunScreen.compact #verify-bar { margin: 0; }
RunScreen.compact #stats { margin-top: 0; }
RunScreen.compact #items { max-height: 4; }
RunScreen.compact #run-actions { height: 1; }
RunScreen.compact #result { height: 1; }
ModalScreen .dialog { max-height: 100%; }
.dlg-scroll { max-height: 50vh; }
""".replace("BORDER", BORDER)


class DiskImgApp(App):
    TITLE = "diskimg"
    SUB_TITLE = f"disk imaging with ddrescue / partclone  v{VERSION}"
    CSS = CSS
    BINDINGS = [Binding("ctrl+q", "quit_guard", "Quit", show=False, priority=True),
                Binding("ctrl+c", "quit_guard", "Quit", show=False, priority=True)]

    def __init__(self):
        super().__init__()
        self.browse_session: BrowseSession | None = None
        self.net_mounted: set[str] = set()
        self.usage: dict = {}
        self.usage_inflight: set = set()
        self.usage_dirty = False

    def on_mount(self) -> None:
        try:
            self.theme = "tokyo-night"
        except Exception:
            pass
        self.push_screen(MainScreen())

    def emergency_quit(self) -> None:
        """SIGHUP/SIGTERM: stop a running job (it exits the app when done), else exit now."""
        for scr in self.screen_stack:
            if isinstance(scr, RunScreen) and scr.running:
                scr.stop_and_quit()
                return
        self.exit()

    def action_quit_guard(self) -> None:
        scr = self.screen
        if isinstance(scr, RunScreen) and scr.running:
            scr.action_quit_app()
        else:
            self.exit()


def missing_tools() -> list[str]:
    return [t for t in ("ddrescue", "lsblk", "findmnt", "blockdev", "sgdisk", "sfdisk", "partx",
                        "blkid", "mkswap", "zstd", "gpg") if not shutil.which(t)]


def _shutdown(app: "DiskImgApp") -> None:
    shutdown_all(app.browse_session, app.net_mounted, report=lambda m: print(m, file=sys.stderr))


def _install_signal_handlers(app: "DiskImgApp") -> None:
    """SIGHUP (terminal closed, SSH dropped) and SIGTERM: stop jobs cleanly instead of leaving
    child processes running on unlocked devices and mounts behind."""
    fired = threading.Event()

    def handler(signum, _frame):
        if fired.is_set():
            return
        fired.set()

        def stopper():
            try:
                app.call_from_thread(app.emergency_quit)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)
            # the UI did not shut down (e.g. no terminal any more): clean up and leave
            _shutdown(app)
            os._exit(128 + signum)
        threading.Thread(target=stopper, daemon=True).start()

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    set_c_locale()
    if os.geteuid() != 0:
        print("diskimg: the text interface must run as root (sudo diskimg --tui).", file=sys.stderr)
        return 1
    missing = missing_tools()
    if missing:
        print(f"diskimg: missing tools: {', '.join(missing)}. Re-run the installer to add them.",
              file=sys.stderr)
        return 1
    for d in (STATE_DIR, MAP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    app = DiskImgApp()
    _install_signal_handlers(app)
    try:
        app.run()
    finally:
        _shutdown(app)
    return 0
