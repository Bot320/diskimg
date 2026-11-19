# SPDX-License-Identifier: GPL-3.0-only
"""End-to-end tests of the job engine that run WITHOUT root: regular files stand in for
partitions. Run:  python3 -I tests/test_jobs.py [WORKDIR]   (WORKDIR must not be on tmpfs)."""
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskimg import engine as E  # noqa: E402

E.set_c_locale()
E.JobRunner._inhibit = lambda self: None           # no systemd-inhibit in tests


class Sink(E.JobSink):
    def __init__(self):
        self.lines, self.steps, self.items = [], {}, {}

    def log(self, m):
        self.lines.append(E.strip_markup(m))

    def step(self, k, s):
        self.steps[k] = s

    def item(self, i, c):
        self.items.setdefault(i, {}).update(c)


def fake_dev(path: Path, fstype="") -> E.Dev:
    return E.Dev(path=str(path), name=path.name, kind="part", size=path.stat().st_size, model="",
                 serial="", tran="", rm=False, ro=False, fstype=fstype, label="", pttype="", mounts=[],
                 depth=0)


def run(job):
    sink = Sink()
    r = E.JobRunner(job, sink)
    res = r.run()
    return res, sink, r


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_ext4(path: Path, size_mib: int, content: Path) -> None:
    with open(path, "wb") as f:
        f.truncate(size_mib * E.MIB)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(content), str(path)], check=True)


def ext4_files(img: Path) -> str:
    """Listing + checksums of all files in an ext4 image (via debugfs, no mount needed)."""
    d = img.parent / (img.name + ".dump")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    r = subprocess.run(["debugfs", "-R", f"rdump / {d}", str(img)], capture_output=True, text=True)
    out = []
    for root, _dirs, files in sorted(os.walk(d)):
        for f in sorted(files):
            p = Path(root) / f
            out.append(f"{p.relative_to(d)} {sha(p)}")
    shutil.rmtree(d)
    return "\n".join(out) or r.stderr


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="diskimg-test-",
                                                                             dir=Path.home()))
    work.mkdir(parents=True, exist_ok=True)
    E.STATE_DIR = work / "state"                 # root-only folders replaced for the test
    E.MAP_DIR = E.STATE_DIR / "maps"
    E.MAP_DIR.mkdir(parents=True, exist_ok=True)
    random.seed(7)
    fails = 0

    def check(cond, what):
        nonlocal fails
        print(("ok    " if cond else "FAIL  ") + what)
        if not cond:
            fails += 1

    # --- filesystem with content ---
    content = work / "content"
    content.mkdir(exist_ok=True)
    for i in range(20):
        (content / f"file{i}.bin").write_bytes(random.randbytes(random.randint(1000, 900_000)))
    (content / "zeros.bin").write_bytes(bytes(5 * E.MIB))      # zeros inside used data
    src = work / "src.ext4"
    make_ext4(src, 64, content)
    want = ext4_files(src)
    devs = {str(src): fake_dev(src, "ext4")}

    for label, pi_extra in (("fs-aware chunked", {"method": "fs"}),
                            ("raw zstd chunked", {"method": "raw", "compression": 3}),
                            ("fs-aware encrypted stream", {"method": "fs", "encrypt": True})):
        dst = work / f"backup-{label.replace(' ', '_')}.dimg"
        shutil.rmtree(dst, ignore_errors=True)
        pi = E.PlanInput(mode="d2i", src_dev=str(src), dst_path=str(dst), **pi_extra)
        job = E.plan_job(pi, devs)
        E.prepare_job(job)
        E.apply_resume_choice(job, None)
        if job.encrypt:
            job.passphrase = "correct horse battery"
        (res, style), sink, _ = run(job)
        check(style == "$success" and res.startswith("SUCCESS"), f"{label}: backup -> {res}")
        check(E.is_container(dst) and not Path(str(dst) + ".partial").exists(), f"{label}: renamed into place")

        # check mode
        cj = E.plan_job(E.PlanInput(mode="chk", src_path=str(dst)), devs)
        if E.passphrase_kind(cj) == "existing":
            check(not E.check_passphrase(cj, "wrong"), f"{label}: wrong passphrase rejected")
            check(E.check_passphrase(cj, "correct horse battery"), f"{label}: right passphrase accepted")
            cj.passphrase = "correct horse battery"
        (res, style), _, _ = run(cj)
        check(style == "$success", f"{label}: check -> {res}")

        # restore into a stale target file of the same size
        tgt = work / "target.img"
        tgt.write_bytes(b"\xa5" * src.stat().st_size)
        tdevs = {str(tgt): fake_dev(tgt)}
        rj = E.plan_job(E.PlanInput(mode="i2d", src_path=str(dst), dst_dev=str(tgt)), tdevs)
        E.prepare_job(rj)
        E.apply_resume_choice(rj, None)
        if E.passphrase_kind(rj) == "existing":
            rj.passphrase = "correct horse battery"
        (res, style), sink, _ = run(rj)
        check(style == "$success" and "verified" in res, f"{label}: restore -> {res}")
        fsck = subprocess.run(["e2fsck", "-fn", str(tgt)], capture_output=True, text=True)
        check(fsck.returncode == 0, f"{label}: e2fsck of restored filesystem (rc {fsck.returncode})")
        check(ext4_files(tgt) == want, f"{label}: restored files identical")

        # damage one byte in the biggest data file -> check must fail, restore must fail
        files = sorted((f for f in dst.iterdir() if f.suffix in (".chunks", ".gpg")),
                       key=lambda f: f.stat().st_size)
        victim = files[-1]
        b = bytearray(victim.read_bytes())
        b[len(b) // 2] ^= 0x40
        victim.write_bytes(b)
        (res, style), _, _ = run(E.plan_job(E.PlanInput(mode="chk", src_path=str(dst)), devs) if not job.encrypt
                                 else _with_pw(E.plan_job(E.PlanInput(mode="chk", src_path=str(dst)), devs)))
        check(style == "$error", f"{label}: damaged backup detected by check -> {res}")
        rj = E.plan_job(E.PlanInput(mode="i2d", src_path=str(dst), dst_dev=str(tgt)), tdevs)
        E.prepare_job(rj)
        E.apply_resume_choice(rj, None)
        if E.passphrase_kind(rj) == "existing":
            rj.passphrase = "correct horse battery"
        (res, style), _, _ = run(rj)
        check(style == "$error", f"{label}: restore from damaged backup refused -> {res}")

    # --- replace an existing backup: old kept until the new one is verified ---
    dst = work / "replace.dimg"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.rmtree(work / "replace.dimg.old", ignore_errors=True)
    for n in range(2):
        job = E.plan_job(E.PlanInput(mode="d2i", src_dev=str(src), dst_path=str(dst), method="fs",
                                     verify=(n == 0)), devs)
        E.prepare_job(job)
        E.apply_resume_choice(job, None)
        (res, style), _, _ = run(job)
    check(res.startswith("DONE - not verified") and (work / "replace.dimg.old").is_dir(),
          f"unverified replacement keeps the previous backup as .old ({res})")
    check(E.is_container(dst), "new backup in place")

    # --- several sources at once (batch): one backup each into a folder
    src2 = work / "src2.ext4"
    make_ext4(src2, 32, content)
    bdevs = {str(src): fake_dev(src, "ext4"), str(src2): fake_dev(src2, "ext4")}
    folder = work / "batch"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    bpi = E.PlanInput(mode="d2i", method="fs", src_devs=[str(src), str(src2)], dst_path=str(folder))
    jobs = E.plan_jobs(bpi, bdevs)
    check(len(jobs) == 2 and len({j.dst for j in jobs}) == 2, f"batch planned: {[Path(j.dst).name for j in jobs]}")
    for j in jobs:
        E.prepare_job(j)
        E.apply_resume_choice(j, None)
    sink = Sink()
    br = E.BatchRunner(jobs, sink)
    res, style = br.run()
    check(style == "$success" and "all 2" in res, f"batch run -> {res}")
    check(all(E.is_container(Path(j.dst)) for j in jobs), "both backups written")
    try:
        E.plan_jobs(E.PlanInput(mode="d2i", src_devs=[str(src), str(src2)], dst_path=str(work / "nope")), bdevs)
        check(False, "batch without existing folder must be refused")
    except ValueError as e:
        check(True, f"batch needs an existing folder ({e})")

    # --- raw image via ddrescue: backup + check, restore with a .map that records bad areas ---
    img = work / "raw.img"
    for f in (img, Path(str(img) + ".map"), Path(str(img) + ".sha256")):
        f.unlink(missing_ok=True)
    job = E.plan_job(E.PlanInput(mode="d2i", src_dev=str(src), dst_path=str(img)), devs)
    E.prepare_job(job)
    E.apply_resume_choice(job, None)
    (res, style), _, _ = run(job)
    check(style == "$success" and sha(img) == sha(src), f"ddrescue raw image -> {res}")
    (res, style), _, _ = run(E.plan_job(E.PlanInput(mode="chk", src_path=str(img)), devs))
    check(style == "$success", f"check raw image -> {res}")
    m = Path(str(img) + ".map")
    lines = m.read_text().splitlines()
    # mark the last 64 KiB as bad-sector ('-') in the mapfile
    size = src.stat().st_size
    keep = [x for x in lines if x.startswith("#") or not x.strip()]
    status = [x for x in lines if x.strip() and not x.startswith("#")][0]
    m.write_text("\n".join(keep + [status, f"0x00000000  0x{size - 65536:08X}  +",
                                   f"0x{size - 65536:08X}  0x00010000  -"]) + "\n")
    (res, style), _, _ = run(E.plan_job(E.PlanInput(mode="chk", src_path=str(img)), devs))
    check(style == "$warning", f"check of image with unreadable areas is a warning -> {res}")
    tgt = work / "target.img"
    tgt.write_bytes(b"\x00" * size)
    tdevs = {str(tgt): fake_dev(tgt)}
    rj = E.plan_job(E.PlanInput(mode="i2d", src_path=str(img), dst_dev=str(tgt)), tdevs)
    rj.mapfile.unlink(missing_ok=True) if rj.mapfile else None
    E.prepare_job(rj)
    E.apply_resume_choice(rj, None)
    (res, style), _, _ = run(rj)
    check(style == "$warning" and "unreadable" in res,
          f"restore of image with unreadable areas is not reported as success -> {res}")

    print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}   (work dir {work})")
    return 1 if fails else 0


def _with_pw(job):
    job.passphrase = "correct horse battery"
    return job


if __name__ == "__main__":
    sys.exit(main())
