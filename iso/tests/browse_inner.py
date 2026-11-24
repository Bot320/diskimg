# SPDX-License-Identifier: GPL-3.0-only
"""Runs INSIDE the rescue-ISO VM as root (started by vm_test.py browse): copying files out of opened
backups as a normal user.

Disks: /dev/vda = work disk (this script, the diskimg code under /mnt/app, results),
/dev/vdb = 640 MiB source disk. Builds a disk with ext4 (private home folder of another user,
root-only files, a device node, a FIFO, symlinks), NTFS and btrfs, backs it up, opens the backup
and checks that the desktop user can read and copy every regular file, that nobody else can, that
nothing is writable and that closing leaves nothing behind. Then the same for a restic file
backup. Results go to /mnt/result.txt."""
import hashlib
import os
import pwd
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/app")
from diskimg import engine as E  # noqa: E402

E.set_c_locale()
SRC = "/dev/vdb"
RESULT = Path("/mnt/result.txt")
USER, OTHER = "tester", "other"
lines: list[str] = []


def say(s: str) -> None:
    print(s, flush=True)
    lines.append(s)
    RESULT.write_text("\n".join(lines) + "\n")


def check(c, what) -> bool:
    say(("ok    " if c else "FAIL  ") + what)
    return bool(c)


def sh(*a, check_rc=True, inp=None):
    r = subprocess.run(a, capture_output=True, text=True, input=inp)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"{' '.join(a)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def as_user(user, *a):
    return subprocess.run(["runuser", "-u", user, "--"] + list(a), capture_output=True, text=True)


def settle(dev):
    sh("partx", "-u", dev, check_rc=False)
    sh("partx", "-a", dev, check_rc=False)
    sh("udevadm", "settle", check_rc=False)
    time.sleep(1)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_sums(top: Path) -> dict:
    out = {}
    for root, _d, fs in os.walk(top):
        for f in fs:
            p = Path(root) / f
            if p.is_symlink() or not p.is_file():
                continue
            out[str(p.relative_to(top))] = sha(p)
    return out


def populate(mp: Path, private: bool, links: bool = True) -> None:
    rnd = random.Random(7)
    home = mp / "home" / "alice"
    home.mkdir(parents=True)
    (home / "Documents").mkdir()
    (home / "Documents" / "secret.txt").write_text("Geheimer Text\n" * 1000)
    (home / "Documents" / "big.bin").write_bytes(rnd.randbytes(24 * 1024 * 1024))
    (home / "Bilder ä ö ü").mkdir()
    (home / "Bilder ä ö ü" / "foto 1.jpg").write_bytes(rnd.randbytes(300_000))
    if links:
        (home / "link-to-secret").symlink_to("Documents/secret.txt")
        (home / "abs-link").symlink_to("/etc/shadow")
    else:                     # NTFS: a Windows-like tree with many small files
        win = mp / "Program Files" / "Some App"
        win.mkdir(parents=True)
        for i in range(400):
            (win / f"datei {i:03d}.dll").write_bytes(rnd.randbytes(rnd.randint(0, 40_000)))
    (mp / "root").mkdir()
    (mp / "root" / "rootonly.txt").write_text("nur root\n")
    if private:
        for root, ds, fs in os.walk(home):
            for n in ds + fs:
                os.lchown(Path(root) / n, 2000, 2000)
        os.chown(home, 2000, 2000)
        os.chmod(home, 0o700)
        os.chmod(home / "Documents" / "secret.txt", 0o600)
        os.chmod(home / "Documents" / "big.bin", 0o600)
        os.chmod(mp / "root", 0o700)
        os.chmod(mp / "root" / "rootonly.txt", 0o600)
        os.mknod(mp / "disk-node", 0o666 | 0o060000, os.makedev(254, 0))    # block device = /dev/vda
        os.mkfifo(mp / "fifo")
        (mp / "suid").write_bytes(Path("/usr/bin/id").read_bytes())
        os.chmod(mp / "suid", 0o4755)


def make_source() -> dict:
    sh("wipefs", "-a", SRC, check_rc=False)
    sh("sgdisk", "-Z", SRC, check_rc=False)
    sh("sgdisk", "-n1:2048:+280M", "-t1:8300", "-c1:Linux", "-n2:0:+150M", "-t2:0700", "-c2:Daten",
       "-n3:0:0", "-t3:8300", "-c3:Btrfs", SRC)
    sh("blockdev", "--rereadpt", SRC, check_rc=False)
    settle(SRC)
    fs = {1: "ext4", 2: "ntfs", 3: "btrfs"}
    for n in fs:
        for _ in range(60):
            if os.path.exists(f"/sys/class/block/vdb{n}") and os.path.exists(f"{SRC}{n}"):
                break
            time.sleep(0.5)
            settle(SRC)
    expect = {}
    for n, t in fs.items():
        dev = f"{SRC}{n}"
        sh("wipefs", "-a", dev, check_rc=False)
        if t == "ntfs":
            sh("mkntfs", "-Q", "-L", "Daten", dev)
        elif t == "btrfs":
            sh("mkfs.btrfs", "-q", "-f", "-L", "Btrfs", dev)
        else:
            sh("mkfs.ext4", "-q", "-F", "-L", "Linux", dev)
        mp = Path(f"/tmp/src{n}")
        mp.mkdir(exist_ok=True)
        if t == "ntfs":
            sh("ntfs-3g", dev, str(mp))
        else:
            sh("mount", "-t", t, dev, str(mp))
        if t == "btrfs":
            sh("btrfs", "subvolume", "create", str(mp / "@home"))
            populate(mp / "@home", private=True)
        else:
            populate(mp, private=(t != "ntfs"), links=(t != "ntfs"))
        expect[n] = tree_sums(mp)
        sh("umount", str(mp))
    sh("sync")
    return expect


def run_job(pi):
    devs = E.load_devices()
    jobs = E.plan_jobs(pi, devs)
    for j in jobs:
        E.prepare_job(j)
        E.apply_resume_choice(j, None)
    runner = E.make_runner(jobs)
    res = runner.run()
    for f in Path("/var/log/diskimg").glob("*.log"):
        shutil.copy(f, f"/mnt/logs/{f.name}")
    return runner, res


def leftovers() -> list[str]:
    out = []
    for ln in Path("/proc/mounts").read_text().splitlines():
        src, mp = ln.split()[:2]
        if "diskimg-view" in src or "/run/diskimg/stage" in mp or "diskimg-browse" in mp \
                or "diskimg-files" in mp or src.startswith("/dev/loop"):
            out.append(f"{src} {mp}")
    if any(Path("/run/diskimg").glob("stage-*")):
        out.append("stage folders left in /run/diskimg")
    return out


def check_view(mp: Path, want: dict, private: bool, tag: str, links: bool = True) -> bool:
    ok = True
    if not links:
        out = Path(pwd.getpwnam(USER).pw_dir) / f"copy-{tag}-pf"
        shutil.rmtree(out, ignore_errors=True)
        r = as_user(USER, "cp", "-r", str(mp / "Program Files"), str(out))
        got = {f"Program Files/{k}": v for k, v in tree_sums(out).items()} if out.exists() else {}
        want_pf = {k: v for k, v in want.items() if k.startswith("Program Files/")}
        ok &= check(r.returncode == 0 and got == want_pf,
                    f"{tag}: {USER} copies 'Program Files' ({len(want_pf)} files identical) {r.stderr.strip()[:200]}")
        shutil.rmtree(out, ignore_errors=True)
        m = os.stat(mp / "root" / "rootonly.txt").st_mode & 0o777
        ok &= check(m == 0o644, f"{tag}: files appear as rw-r--r-- (not executable): {oct(m)}")
    out = Path(pwd.getpwnam(USER).pw_dir) / f"copy-{tag}"
    shutil.rmtree(out, ignore_errors=True)
    r = as_user(USER, "cp", "-r", "--no-preserve=all", str(mp / "home"), str(out))
    ok &= check(r.returncode == 0, f"{tag}: {USER} copies the whole home folder (cp -r) {r.stderr.strip()[:200]}")
    got = {f"home/{k}": v for k, v in tree_sums(out).items()} if out.exists() else {}
    want_home = {k: v for k, v in want.items() if k.startswith("home/")}
    ok &= check(got == want_home, f"{tag}: all {len(want_home)} copied files identical")
    ok &= check(as_user(USER, "cat", str(mp / "root" / "rootonly.txt")).stdout == "nur root\n",
                f"{tag}: root-only file readable by {USER}")
    if links:
        ok &= check(as_user(USER, "readlink", str(mp / "home/alice/link-to-secret")).stdout.strip()
                    == "Documents/secret.txt", f"{tag}: symlink kept")
        ok &= check(as_user(USER, "cat", str(mp / "home/alice/abs-link")).returncode != 0,
                    f"{tag}: absolute symlink to /etc/shadow is not readable by {USER}")
    ok &= check(as_user(USER, "touch", str(mp / "new")).returncode != 0, f"{tag}: not writable")
    ok &= check(as_user(USER, "rm", "-f", str(mp / "root" / "rootonly.txt")).returncode != 0
                and (mp / "root" / "rootonly.txt").exists(), f"{tag}: files cannot be deleted")
    if private:
        ok &= check(as_user(OTHER, "ls", str(mp / "home")).returncode != 0,
                    f"{tag}: another user ({OTHER}) cannot look inside")
        r = as_user(USER, "dd", f"if={mp / 'disk-node'}", "of=/dev/null", "bs=512", "count=1")
        ok &= check(r.returncode != 0, f"{tag}: device node from the backup cannot be opened")
        r = as_user(USER, "timeout", "5", "cat", str(mp / "fifo"))
        ok &= check(r.returncode not in (0, 124), f"{tag}: FIFO does not block (refused)")
        r = as_user(USER, str(mp / "suid"), "-u")
        ok &= check(r.stdout.strip() != "0", f"{tag}: set-uid program from the backup does not run as root")
    shutil.rmtree(out, ignore_errors=True)
    return ok


def disk_backup() -> bool:
    expect = make_source()
    ok = True
    for method in ("fs", "raw"):
        bk = f"/mnt/browse-{method}.dimg"
        shutil.rmtree(bk, ignore_errors=True)
        _r, (res, style) = run_job(E.PlanInput(mode="d2i", method=method, src_dev=SRC, dst_path=bk))
        ok &= check(style == "$success", f"{method} backup: {res}")
        ids = [r["id"] for r in E.browse_rows_for(bk) if r["ok"]]
        runner, (res, style) = run_job(E.PlanInput(mode="brw", src_path=bk, browse_ids=ids))
        ok &= check(style == "$success", f"{method}: open backup ({', '.join(ids)}): {res}")
        sess = runner.browse_session
        if sess is None:
            return False
        mounts = {desc.split()[0]: Path(mp) for mp, desc in sess.mounts}
        ok &= check(set(mounts) == {"ext4", "ntfs", "btrfs"}, f"{method}: mounted {sorted(mounts)}")
        for fs, n in (("ext4", 1), ("ntfs", 2), ("btrfs", 3)):
            if fs not in mounts:
                continue
            mp = mounts[fs]
            st = os.stat(mp)
            ok &= check(st.st_uid == pwd.getpwnam(USER).pw_uid, f"{method}/{fs}: top folder belongs to {USER}")
            if fs == "btrfs":
                want = {k[len("@home/"):]: v for k, v in expect[n].items() if k.startswith("@home/")}
                ok &= check_view(mp / "@home", want, True, f"{method}/{fs}")
            else:
                ok &= check_view(mp, expect[n], fs != "ntfs", f"{method}/{fs}", links=(fs != "ntfs"))
            if fs == "ntfs":
                drv = next((ln.split()[2] for ln in Path("/proc/mounts").read_text().splitlines()
                            if ln.split()[1] == str(mp)), "?")
                ok &= check(drv == "ntfs3", f"{method}/ntfs: opened with the kernel driver ntfs3 "
                                           f"(reads CompactOS files): {drv}")
        # a program still using the backup: closing must refuse, then detach on request
        busy = subprocess.Popen(["runuser", "-u", USER, "--", "sleep", "30"], cwd=str(mounts["ext4"]))
        time.sleep(1)
        errs = E.close_browse(sess, force=False)
        ok &= check(bool(errs), f"{method}: close refused while a program uses the backup")
        busy.kill()
        busy.wait()
        time.sleep(1)
        errs = E.close_browse(sess, force=False)
        ok &= check(not errs, f"{method}: closed {'; '.join(errs)}")
        time.sleep(2)
        lo = leftovers()
        ok &= check(not lo, f"{method}: nothing left mounted {lo}")
        shutil.rmtree(bk, ignore_errors=True)
    return ok


def restic_backup() -> bool:
    if not shutil.which("restic"):
        return check(False, "restic not installed")
    data = Path("/tmp/resticdata")
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir()
    populate(data, private=False)
    home = data / "home" / "alice"
    for root, ds, fs in os.walk(home):
        for n in ds + fs:
            os.lchown(Path(root) / n, 2000, 2000)
    os.chmod(home, 0o700)
    os.chmod(home / "Documents" / "secret.txt", 0o600)
    os.chmod(data / "root", 0o700)
    os.chmod(data / "root" / "rootonly.txt", 0o600)
    want = tree_sums(data)
    s = {"id": "vmtest", "name": "VM test", "repo": "/mnt/restic-repo"}
    shutil.rmtree(s["repo"], ignore_errors=True)
    E.write_secret(E.restic_pwfile(s), "pw-vmtest")
    sh(*E.restic_base(s), "init")
    sh(*E.restic_base(s), "backup", str(data))
    sess, err = E.start_restic_mount(s)
    ok = check(sess is not None, f"restic: versions opened {err or ''}")
    if sess is None:
        return False
    mp = Path(sess.mounts[0][0])
    latest = mp / "snapshots" / "latest" / str(data).lstrip("/")
    ok &= check(as_user(USER, "ls", str(latest)).returncode == 0, f"restic: {USER} sees the latest version")
    ok &= check_view(latest, want, False, "restic")
    ok &= check(as_user(OTHER, "ls", str(mp)).returncode != 0, f"restic: {OTHER} cannot look inside")
    errs = E.close_browse(sess)
    ok &= check(not errs, f"restic: closed {'; '.join(errs)}")
    time.sleep(2)
    lo = leftovers()
    ok &= check(not lo, f"restic: nothing left mounted {lo}")
    shutil.rmtree(s["repo"], ignore_errors=True)
    return ok


def main() -> int:
    os.makedirs("/mnt/logs", exist_ok=True)
    for u, uid in ((USER, 1000), (OTHER, 1500)):
        sh("useradd", "-m", "-u", str(uid), u, check_rc=False)
    os.environ["SUDO_USER"] = USER          # diskimg started by this desktop user
    for d in (E.STATE_DIR, E.MAP_DIR, E.LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, fn in (("disk backups", disk_backup), ("file backup", restic_backup)):
        say(f"=== {name} ===")
        try:
            ok &= fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            say(traceback.format_exc())
            ok = check(False, f"{name}: {type(e).__name__}: {e}")
    say("BROWSE VM TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["sync"])
