# SPDX-License-Identifier: GPL-3.0-only
"""Runs INSIDE the rescue-ISO VM as root (started by vm_test.py desktop) while the graphical start
menu runs on tty1: NAS shares in the file manager, GNOME's file chooser.

Installs samba and grim from the internet (test only), shares a folder over SMB on this machine,
connects it with diskimg and checks that GIO (Nautilus) lists it with the drives, under a name that
says NAS. Then opens a GTK file dialog in the live session and checks that the GNOME portal
(Nautilus) shows it. Screenshots go to /mnt/shots, results to /mnt/result.txt."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/app")
from diskimg import engine as E  # noqa: E402

E.set_c_locale()
RESULT = Path("/mnt/result.txt")
SHOTS = Path("/mnt/shots")
lines: list[str] = []


def say(s: str) -> None:
    print(s, flush=True)
    lines.append(s)
    RESULT.write_text("\n".join(lines) + "\n")


def check(c, what) -> bool:
    say(("ok    " if c else "FAIL  ") + what)
    return bool(c)


def sh(*a, check_rc=True, timeout=600):
    r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"{' '.join(a)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def session_env() -> dict:
    """Environment of the live user's graphical session (the start menu process)."""
    for _ in range(60):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"rescue-launcher" not in cmd and b"launcher.py" not in cmd:
                    continue
                env = dict(x.split("=", 1) for x in
                           Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if "=" in x)
            except OSError:
                continue
            if env.get("WAYLAND_DISPLAY") and env.get("DBUS_SESSION_BUS_ADDRESS"):
                return env
        time.sleep(2)
    raise RuntimeError("graphical session not found")


def live(env: dict, *a, bg=False, timeout=60):
    keep = {k: env[k] for k in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                                "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "XKB_DEFAULT_LAYOUT", "HOME", "PATH")
            if k in env}
    cmd = ["runuser", "-u", "live", "--", "env"] + [f"{k}={v}" for k, v in keep.items()] + list(a)
    if bg:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def shot(env: dict, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    tmp = f"/tmp/{name}.png"
    r = live(env, "grim", tmp)
    if r.returncode == 0:
        shutil.copy(tmp, SHOTS / f"{name}.png")
        say(f"      screenshot {name}.png")
    else:
        say(f"      (no screenshot: {r.stderr.strip()[:200]})")


def nas_test(env: dict) -> bool:
    share = Path("/srv/share")
    share.mkdir(parents=True, exist_ok=True)
    (share / "hello.txt").write_text("hallo vom NAS\n")
    os.chmod(share, 0o777)
    Path("/etc/samba").mkdir(exist_ok=True)
    Path("/etc/samba/smb.conf").write_text(
        "[global]\n  map to guest = Bad User\n  server min protocol = SMB2\n  smb ports = 445\n"
        "[backups]\n  path = /srv/share\n  guest ok = yes\n  read only = no\n  force user = root\n")
    for d in ("/var/lib/samba/private", "/var/cache/samba", "/run/samba", "/var/log/samba"):
        os.makedirs(d, exist_ok=True)
    sh("smbd", "-D")
    time.sleep(3)
    conn = {"proto": "smb", "server": "127.0.0.1", "share": "backups", "user": "", "vers": "auto"}
    mp = E.mount_share(conn, "")
    ok = check(mp == "/media/NAS 127.0.0.1 backups", f"share mounted at {mp!r}")
    ok &= check(Path(mp, "hello.txt").read_text() == "hallo vom NAS\n", "share readable")
    net = E.net_share_of(mp)
    ok &= check(net == "SMB //127.0.0.1/backups", f"recognized as network share: {net}")
    old = E.OLD_NET_ROOT / "127.0.0.1_backups"
    ok &= check(old.is_symlink() and Path(old, "hello.txt").exists(),
                f"old path {old} still leads to the share")
    lt = E.location_text(mp)
    ok &= check("NAS" in lt, f"location text: {lt}")
    # what Nautilus shows: GIO's list of mounts of the live user
    r = live(env, "gio", "mount", "-l")
    ok &= check("NAS 127.0.0.1 backups" in r.stdout, "GIO / Nautilus lists the share with the drives "
                f"({' | '.join(x.strip() for x in r.stdout.splitlines() if 'Mount' in x)[:300]})")
    fm = live(env, "nautilus", "--new-window", "computer:///", bg=True)
    time.sleep(8)
    shot(env, "nautilus-this-pc")
    sh("pkill", "-u", "live", "-f", "nautilus", check_rc=False)
    fm.wait(timeout=30)
    # a backup target on the share is marked as NAS when the job is planned
    devs = E.load_devices()
    src = next((d for d in devs.values() if d.kind == "disk" and d.path != "/dev/vda"
                and not d.mounts), None)
    if src is not None:
        try:
            jobs = E.plan_jobs(E.PlanInput(mode="d2i", method="raw", src_dev=src.path,
                                           dst_path=f"{mp}/test.img"), devs)
            ok &= check("NAS" in jobs[0].dst_label, f"target label: {jobs[0].dst_label}")
            info = " ".join(E.JobRunner(jobs[0]).info_lines())
            ok &= check("network share" in info, "job screen says the target is a network share")
        except Exception as e:  # noqa: BLE001
            ok &= check(False, f"planning a backup onto the share: {e}")
    e = E.umount_path(mp)
    ok &= check(e is None and not os.path.exists(mp), f"share unmounted {e or ''}")
    sh("pkill", "smbd", check_rc=False)
    return ok


DIALOG = r'''
import sys
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
app = Gtk.Application(application_id="org.example.dialogtest")
def done(d, res):
    try:
        f = d.select_folder_finish(res)
        print("CHOSEN", f.get_path() if f else None, flush=True)
    except GLib.Error as e:
        print("ERROR", e.message, flush=True)
    app.quit()
def act(a):
    w = Gtk.ApplicationWindow(application=a, title="Dialogtest")
    w.present()
    Gtk.FileDialog(title="Backup-Ordner waehlen").select_folder(w, None, done)
app.connect("activate", act)
app.hold()
app.run([])
'''


def chooser_test(env: dict) -> bool:
    r = live(env, "gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop", "--method",
             "org.freedesktop.DBus.Properties.Get", "org.freedesktop.portal.FileChooser", "version",
             timeout=60)
    ok = check(env.get("XDG_SESSION_TYPE") == "wayland",
               f"session type is wayland: {env.get('XDG_SESSION_TYPE')}")
    ok &= check(r.returncode == 0, f"file chooser portal answers: {(r.stdout or r.stderr).strip()[:200]}")
    Path("/tmp/dialog.py").write_text(DIALOG)
    os.chmod("/tmp/dialog.py", 0o644)
    # verbose logs of the portal chain: xdg-desktop-portal -> xdg-desktop-portal-gnome -> Nautilus
    sh("pkill", "-u", "live", "-f", "nautilus", check_rc=False)
    logs = {}
    for name, cmd in (("xdp", ["/usr/lib/xdg-desktop-portal", "--replace", "--verbose"]),
                      ("xdpg", ["/usr/lib/xdg-desktop-portal-gnome", "--replace", "--verbose"]),
                      ("nautilus", ["nautilus", "--gapplication-service"])):
        f = open(f"/mnt/logs/{name}.log", "w")
        logs[name] = f
        keep = {k: env[k] for k in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                                    "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "HOME", "PATH") if k in env}
        subprocess.Popen(["runuser", "-u", "live", "--", "env", "G_MESSAGES_DEBUG=all"]
                         + [f"{k}={v}" for k, v in keep.items()] + cmd,
                         stdout=f, stderr=subprocess.STDOUT)
        time.sleep(3)
    p = live(env, "env", "G_MESSAGES_DEBUG=all", "python3", "/tmp/dialog.py", bg=True)
    time.sleep(12)
    shot(env, "file-chooser")
    for name, f in logs.items():
        f.flush()
        txt = Path(f"/mnt/logs/{name}.log").read_text(errors="replace")
        bad = [ln for ln in txt.splitlines()
               if any(w in ln.lower() for w in ("fail", "error", "critical", "warning", "cannot", "not "))]
        say(f"      {name}.log ({len(txt.splitlines())} lines): " + " || ".join(bad[-6:])[:900])
    procs = sh("ps", "-eo", "user,args", check_rc=False).stdout
    gnome = [ln for ln in procs.splitlines() if "xdg-desktop-portal-gnome" in ln]
    naut = [ln for ln in procs.splitlines() if "nautilus" in ln and "live" in ln]
    ok &= check(bool(gnome), "GNOME portal running: " + (gnome[0].strip()[:120] if gnome else "no"))
    ok &= check(bool(naut), "Nautilus provides the dialog: " + (naut[0].strip()[:120] if naut else "no"))
    sh("pkill", "-u", "live", "-f", "dialog.py", check_rc=False)
    sh("pkill", "-u", "live", "-f", "nautilus", check_rc=False)
    try:
        out, _ = p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        out = ""
    Path("/mnt/logs/dialog.log").write_text(out or "")
    ok &= check("Dismissed" not in (out or "") and "ERROR" not in (out or ""),
                "the dialog was still open when it was closed by the test")
    say("      dialog program output: " + " ".join(
        ln for ln in (out or "").splitlines() if "CHOSEN" in ln or "ERROR" in ln or "portal" in ln.lower())[:600])
    return ok


def main() -> int:
    os.makedirs("/mnt/logs", exist_ok=True)
    for d in (E.STATE_DIR, E.LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    ok = True
    try:
        env = session_env()
        sh("pacman", "-Sy", "--noconfirm", "--needed", "samba", "grim", timeout=900)
    except Exception as e:  # noqa: BLE001
        say(f"FAIL  setup: {e}")
        say("DESKTOP VM TEST FAILED")
        return 1
    sh("chvt", "1", check_rc=False)        # show the graphical session (screenshots need it active)
    time.sleep(3)
    shot(env, "start-menu")
    for name, fn in (("NAS in the file manager", nas_test), ("file chooser", chooser_test)):
        say(f"=== {name} ===")
        try:
            ok &= fn(env)
        except Exception as e:  # noqa: BLE001
            import traceback
            say(traceback.format_exc())
            ok = check(False, f"{name}: {type(e).__name__}: {e}")
    say("DESKTOP VM TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["sync"])
