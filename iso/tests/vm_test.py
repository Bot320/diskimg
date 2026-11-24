#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-only
"""Automated VM tests on the rescue ISO. Run on the host (outside any sandbox; needs KVM and QEMU):

    python3 -I iso/tests/vm_test.py ISO TEST [TEST ...] [--work DIR]

TEST: shrink   restore onto a smaller disk (shrink_inner.py)
      browse   copy files out of opened backups as a normal user (browse_inner.py)
      desktop  NAS shares in the file manager, GNOME file chooser (desktop_inner.py; needs internet
               in the VM for samba and grim)
Boots the ISO with a work disk (this repository's diskimg code + the test script) and the test's
disks, runs the test as root inside the VM and prints the result; screenshots are copied to the
work folder."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VM = [sys.executable, "-I", str(HERE / "vm.py")]
TESTS = {"shrink": ("shrink_inner.py", {"src.img": 640, "tgt.img": 560}),
         "browse": ("browse_inner.py", {"src.img": 640}),
         "desktop": ("desktop_inner.py", {"src.img": 64})}


def vm(*a):
    return subprocess.run(VM + list(a), capture_output=True, text=True)


def main() -> int:
    args = sys.argv[1:]
    work_arg = None
    if "--work" in args:
        i = args.index("--work")
        work_arg = args[i + 1]
        del args[i:i + 2]
    if len(args) < 2 or any(t not in TESTS for t in args[1:]):
        sys.exit(__doc__)
    rc = 0
    for test in args[1:]:
        work = Path(work_arg or REPO.parent / f"{test}-vmtest").resolve()
        rc |= run_test(Path(args[0]).resolve(), test, work)
    return rc


def run_test(iso: Path, test: str, work: Path) -> int:
    inner, extra = TESTS[test]
    print(f"===== {test}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    (work / "wd").mkdir(parents=True)
    shutil.copytree(REPO / "diskimg", work / "wd" / "app" / "diskimg",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "libexec", work / "wd" / "app" / "libexec")
    shutil.copy(HERE / inner, work / "wd" / "inner.py")
    (work / "wd" / "run.sh").write_text("python3 -I /mnt/inner.py > /mnt/inner.out 2>&1\nsync\npoweroff\n")
    disks = {"work.img": 2048, **extra}
    need = sum(disks.values()) - 300
    free = shutil.disk_usage(work).free
    if free < need * 1024 * 1024:
        print(f"not enough free space for the test disks in {work}: {free // 2**20} MiB (needs {need})")
        return 1
    for name, mib in disks.items():
        with open(work / name, "wb") as f:
            f.truncate(mib * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "WORK", "-d", str(work / "wd"), str(work / "work.img")],
                   check=True)
    print(f"starting VM with {iso.name} ...", flush=True)
    r = vm("start", str(iso), *[a for name in disks for a in ("--disk", str(work / name))])
    if r.returncode != 0:
        print(r.stderr)
        return 1
    vmdir = os.environ.get("VM_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "diskimg-vm")
    pid = int(Path(vmdir, "pid").read_text())
    time.sleep(75)                                   # boot into the start menu
    for a in (("key", "ctrl-alt-f2"), ("type", "root", "--layout", "de"), ("key", "ret")):
        vm(*a)
        time.sleep(2)
    vm("type", "mount /dev/vda /mnt", "--layout", "de")
    vm("key", "ret")
    time.sleep(2)
    vm("type", "sh /mnt/run.sh", "--layout", "de")
    vm("key", "ret")
    print("test running inside the VM (several minutes) ...", flush=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3600:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(10)
    else:
        vm("stop")
        print("timeout: the VM did not finish")
    for f in ("result.txt", "inner.out"):
        out = subprocess.run(["debugfs", "-R", f"cat /{f}", str(work / "work.img")],
                             capture_output=True, text=True).stdout
        print(f"----- {f}\n{out}")
    res = subprocess.run(["debugfs", "-R", "cat /result.txt", str(work / "work.img")],
                         capture_output=True, text=True).stdout
    shots = subprocess.run(["debugfs", "-R", "ls -l /shots", str(work / "work.img")],
                           capture_output=True, text=True).stdout
    for name in [ln.split()[-1] for ln in shots.splitlines() if ln.strip().endswith(".png")]:
        subprocess.run(["debugfs", "-R", f"dump /shots/{name} {work / name}", str(work / "work.img")],
                       capture_output=True)
        print(f"screenshot: {work / name}")
    print(f"logs: debugfs -R 'ls /logs' {work / 'work.img'}")
    return 0 if "VM TEST PASSED" in res else 1


if __name__ == "__main__":
    sys.exit(main())
