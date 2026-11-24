# SPDX-License-Identifier: GPL-3.0-only
"""Runs INSIDE the rescue-ISO VM as root (started by shrink_vm_test.py): restore onto a smaller disk.

Disks: /dev/vda = work disk (this script, the diskimg code under /mnt/app, results),
/dev/vdb = 640 MiB source disk, /dev/vdc = 560 MiB target disk.
For two layouts: partition and fill the source, back it up (filesystem-aware), restore it onto the
smaller target, then compare every file and check the filesystems. Results go to /mnt/result.txt."""
import hashlib
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/app")
from diskimg import engine as E  # noqa: E402

E.set_c_locale()
SRC, TGT = "/dev/vdb", "/dev/vdc"
RESULT = Path("/mnt/result.txt")
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


def settle(dev):
    sh("partx", "-u", dev, check_rc=False)
    sh("partx", "-a", dev, check_rc=False)
    sh("udevadm", "settle", check_rc=False)
    time.sleep(1)


def fill(mp: Path, mb: int, seed: int) -> None:
    """Fill a filesystem, then delete every second file: data ends up spread over the partition."""
    rnd = random.Random(seed)
    d = mp / "data"
    d.mkdir()
    files = []
    for i in range(mb // 4):
        f = d / f"f{i:04d}.bin"
        try:
            f.write_bytes(rnd.randbytes(4 * 1024 * 1024))
        except OSError:
            break
        files.append(f)
    for f in files[::2]:
        f.unlink()
    (mp / "hello.txt").write_text("Hallo von der Quellplatte\n" * 100)


def sums(mp: Path) -> dict:
    out = {}
    for root, _d, fs in os.walk(mp):
        for f in fs:
            p = Path(root) / f
            if "System Volume Information" in str(p) or "lost+found" in str(p):
                continue
            out[str(p.relative_to(mp))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def mount(dev, fs, mp, ro=False):
    os.makedirs(mp, exist_ok=True)
    if fs == "ntfs":
        sh("ntfs-3g", "-o", "ro" if ro else "rw", dev, mp)
    else:
        sh("mount", "-t", fs, "-o", "ro" if ro else "rw", dev, mp)


def umount(mp):
    sh("umount", mp, check_rc=False)


def make_source(scenario: str) -> dict:
    sh("wipefs", "-a", SRC, check_rc=False)
    sh("sgdisk", "-Z", SRC, check_rc=False)
    if scenario == "ntfs":     # Windows-like: EFI, MSR, C: (NTFS, big), Linux data (ext4) at the end
        sh("sgdisk", "-n1:2048:+100M", "-t1:ef00", "-n2:0:+16M", "-t2:0c01", "-n3:0:-128M", "-t3:0700",
           "-c3:Windows", "-n4:0:0", "-t4:8300", "-c4:Linux", SRC)
        fs = {1: "vfat", 3: "ntfs", 4: "ext4"}
    else:                      # Linux-like: EFI, root (ext4, big), Windows data (NTFS) at the end
        sh("sgdisk", "-n1:2048:+100M", "-t1:ef00", "-n2:0:-128M", "-t2:8300", "-c2:Root",
           "-n3:0:0", "-t3:0700", "-c3:Data", SRC)
        fs = {1: "vfat", 2: "ext4", 3: "ntfs"}
    sh("blockdev", "--rereadpt", SRC, check_rc=False)
    settle(SRC)
    for n in fs:                     # wait until the kernel and udev show every new partition
        for _ in range(60):
            dev = f"{SRC}{n}"
            if os.path.exists(dev) and os.path.exists(f"/sys/class/block/{Path(dev).name}"):
                break
            time.sleep(0.5)
            settle(SRC)
        else:
            raise RuntimeError(f"{SRC}{n} did not appear")
    expect = {}
    for n, t in fs.items():
        dev = f"{SRC}{n}"
        sh("wipefs", "-a", dev, check_rc=False)
        if t == "vfat":
            sh("mkfs.fat", "-F32", "-n", "EFI", dev)
        elif t == "ntfs":
            sh("mkntfs", "-Q", "-L", "Windows" if scenario == "ntfs" else "Data", dev)
        else:
            sh("mkfs.ext4", "-q", "-F", "-L", "Linux" if scenario == "ntfs" else "Root", dev)
        got = sh("blkid", "-p", "-o", "value", "-s", "TYPE", dev, check_rc=False).stdout.strip()
        if got != t:
            raise RuntimeError(f"{dev}: formatted as {t}, but blkid sees {got!r}")
        mp = f"/tmp/src{n}"
        mount(dev, t, mp)
        size_mb = int(sh("blockdev", "--getsize64", dev).stdout) // (1024 * 1024)
        fill(Path(mp), int(size_mb * (0.6 if t != "vfat" else 0.3)), seed=n)
        expect[n] = (t, sums(Path(mp)))
        umount(mp)
    sh("sync")
    return expect


class BarSink(E.JobSink):
    """Highest percentage each progress bar / the "Total" value showed. With press="skip"/"stop",
    presses that button as soon as the check before shrinking is running."""
    def __init__(self, press: str | None = None):
        self.top: dict[str, float] = {}
        self.press, self.pressed, self.runner, self.logs = press, None, None, []
        self.buttons: dict[str, bool] = {}

    def log(self, markup):
        self.logs.append(markup)

    def button(self, name, enabled):
        self.buttons[name] = enabled

    def bar(self, which, pct, info=None):
        self.top[which] = max(self.top.get(which, 0.0), pct)
        r = self.runner
        if self.press and self.pressed is None and which == "verify" and r and r.steps.get("copy") == "run":
            self.pressed = (r.request_skip() if self.press == "skip" else (r.request_stop() or True),
                            self.buttons.get("skip"))

    def pairs(self, pairs):
        for k, v, _a in pairs:
            if k == "Total":
                self.top["Total"] = max(self.top.get("Total", 0.0), float(v.strip().rstrip("%")))


LAST_SINK = BarSink()


def run_job(pi, press: str | None = None) -> tuple:
    global LAST_SINK
    devs = E.load_devices()
    jobs = E.plan_jobs(pi, devs)
    for j in jobs:
        E.prepare_job(j)
        E.apply_resume_choice(j, None)
    LAST_SINK = BarSink(press)
    runner = E.make_runner(jobs, LAST_SINK)
    LAST_SINK.runner = runner
    res = runner.run()
    for f in Path("/var/log/diskimg").glob("*.log"):
        shutil.copy(f, f"/mnt/logs/{f.name}")
    return jobs, res


def scenario(name: str, press: str | None = None) -> bool:
    say(f"=== scenario {name}: shrink the {name} partition"
        + (f", press {press} during the check before shrinking" if press else "") + " ===")
    expect = make_source(name)
    bk = f"/mnt/backup-{name}.dimg"
    shutil.rmtree(bk, ignore_errors=True)
    _jobs, (res, style) = run_job(E.PlanInput(mode="d2i", method="fs", src_dev=SRC, dst_path=bk))
    ok = check(style == "$success", f"backup: {res}")
    sh("wipefs", "-a", TGT, check_rc=False)
    jobs, (res, style) = run_job(E.PlanInput(mode="i2d", src_path=bk, dst_dev=TGT), press)
    sp = jobs[0].shrink
    ok &= check(sp is not None, f"restore planned with shrinking: partition {sp.part if sp else '-'} "
                                f"{E.human(sp.old_size) if sp else ''} -> {E.human(sp.new_size) if sp else ''}")
    logtxt = "\n".join(LAST_SINK.logs)
    if press:
        ok &= check(LAST_SINK.pressed is not None and LAST_SINK.pressed[0] and LAST_SINK.pressed[1],
                    f"{press} button enabled and accepted during the check before shrinking: {LAST_SINK.pressed}")
    if press == "stop":
        ok &= check(res == "STOPPED", f"Stop stops the restore: {res}")
        ok &= check("made smaller to" not in logtxt, "nothing was made smaller after Stop")
        shutil.rmtree(bk, ignore_errors=True)
        return ok
    if press == "skip":
        ok &= check(res.startswith("DONE - not verified"), f"result says not verified: {res}")
        ok &= check("not checked:" in logtxt, "the other partitions were still checked")
        ok &= check("made smaller to" in logtxt, "partition made smaller after the skipped check")
    else:
        ok &= check(style == "$success", f"restore onto the 560 MiB disk: {res}")
    top = LAST_SINK.top
    ok &= check(top and max(top.values()) <= 100.05,
                "progress never above 100 %: " + ", ".join(f"{k} {v:.1f} %" for k, v in sorted(top.items())))
    settle(TGT)
    ok &= check(sh("sfdisk", "--verify", TGT, check_rc=False).returncode == 0, "partition table verifies")
    for n, (t, want) in expect.items():
        dev = f"{TGT}{n}"
        if t == "ntfs":
            ok &= check(sh("ntfsresize", "-f", "-i", "-P", dev, check_rc=False).returncode == 0,
                        f"partition {n}: NTFS consistent")
        elif t == "ext4":
            ok &= check(sh("e2fsck", "-f", "-n", dev, check_rc=False).returncode == 0,
                        f"partition {n}: ext4 consistent")
        mp = f"/tmp/tgt{n}"
        mount(dev, t, mp, ro=True)
        got = sums(Path(mp))
        umount(mp)
        ok &= check(got == want, f"partition {n} ({t}): all {len(want)} files identical")
    shutil.rmtree(bk, ignore_errors=True)            # keep the work disk small
    return ok


def main() -> int:
    os.makedirs("/mnt/logs", exist_ok=True)
    if not shutil.which("mkntfs") or not shutil.which("ntfsresize"):
        say("installing ntfsprogs (this ISO does not include it yet) ...")
        sh("pacman", "-Sy", "--noconfirm", "--needed", "ntfsprogs")
    E.STATE_DIR.mkdir(parents=True, exist_ok=True)
    E.MAP_DIR.mkdir(parents=True, exist_ok=True)
    E.LOG_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, press in (("ntfs", None), ("ext4", None), ("ntfs", "skip"), ("ntfs", "stop")):
        try:
            ok &= scenario(name, press)
        except Exception as e:  # noqa: BLE001
            ok = check(False, f"scenario {name} {press or ''}: {type(e).__name__}: {e}")
    say("SHRINK VM TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["sync"])
