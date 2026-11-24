#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-only
"""Automated VM test of restoring onto a smaller disk. Run on the host (outside any sandbox; needs
KVM and QEMU):   python3 -I iso/tests/shrink_vm_test.py ISO [WORKDIR]

Boots the rescue ISO with a work disk (this repository's diskimg code + shrink_inner.py), a 1 GiB
source disk and a 900 MiB target disk, runs the test as root inside the VM and prints the result."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VM = [sys.executable, "-I", str(HERE / "vm.py")]


def vm(*a):
    return subprocess.run(VM + list(a), capture_output=True, text=True)


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    iso = Path(sys.argv[1]).resolve()
    work = Path(sys.argv[2] if len(sys.argv) > 2 else REPO.parent / "shrink-vmtest").resolve()
    shutil.rmtree(work, ignore_errors=True)
    (work / "wd").mkdir(parents=True)
    shutil.copytree(REPO / "diskimg", work / "wd" / "app" / "diskimg",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(HERE / "shrink_inner.py", work / "wd" / "inner.py")
    (work / "wd" / "run.sh").write_text("python3 -I /mnt/inner.py > /mnt/inner.out 2>&1\nsync\npoweroff\n")
    disks = {"work.img": 3072, "src.img": 1024, "tgt.img": 900}
    for name, mib in disks.items():
        with open(work / name, "wb") as f:
            f.truncate(mib * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "WORK", "-d", str(work / "wd"), str(work / "work.img")],
                   check=True)
    print(f"starting VM with {iso.name} ...", flush=True)
    r = vm("start", str(iso), "--disk", str(work / "work.img"), "--disk", str(work / "src.img"),
           "--disk", str(work / "tgt.img"))
    if r.returncode != 0:
        sys.exit(r.stderr)
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
    print(f"logs: debugfs -R 'ls /logs' {work / 'work.img'}")
    return 0 if "SHRINK VM TEST PASSED" in res else 1


if __name__ == "__main__":
    sys.exit(main())
