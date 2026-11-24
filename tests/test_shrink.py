# SPDX-License-Identifier: GPL-3.0-only
"""Restore onto a smaller disk: planning, partition-table rewrite (real sfdisk on image files) and
ext4 shrinking on an image file. No root needed.  Usage: python3 -I tests/test_shrink.py WORKDIR"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskimg import engine as E  # noqa: E402

E.set_c_locale()
MIB = E.MIB
fails = 0


def check(c, what):
    global fails
    print(("ok    " if c else "FAIL  ") + what)
    fails += 0 if c else 1


def sh(*a, inp=None):
    return subprocess.run(a, input=inp, capture_output=True, text=True)


def layout_of(img: Path):
    """A Layout like analyze() makes, from a disk image file (no kernel partitions needed)."""
    pt = json.loads(sh("sfdisk", "-J", str(img)).stdout)["partitiontable"]
    ss = pt.get("sectorsize", 512)
    parts = [E.PartInfo(int(p["node"][len(str(img)):]), p["node"], int(p["start"]) * ss, int(p["size"]) * ss)
             for p in pt["partitions"]]
    end = max(p.start + p.size for p in parts)
    return E.Layout("disk", pt["label"], ss, img.stat().st_size, parts, boot_len=MIB,
                    needed=end + 34 * ss, sfdisk_dump=sh("sfdisk", "-d", str(img)).stdout)


def main():
    w = Path(sys.argv[1])
    shutil.rmtree(w, ignore_errors=True)
    w.mkdir(parents=True)
    src = w / "src.img"
    src.write_bytes(b"")
    with open(src, "r+b") as f:
        f.truncate(1024 * MIB)
    # Windows-like disk: EFI 100M, MSR 16M, C: (big), recovery 300M at the very end
    r = sh("sgdisk", "-n1:2048:+100M", "-t1:ef00", "-n2:0:+16M", "-t2:0c01", "-n3:0:-300M", "-t3:0700",
           "-c3:Windows", "-n4:0:0", "-t4:2700", "-c4:Recovery", str(src))
    check(r.returncode == 0, "source disk image partitioned (GPT, 4 partitions)")
    lay = layout_of(src)
    for p, fs in zip(lay.parts, ("vfat", "", "ntfs", "ntfs")):
        p.fstype = fs
    items = [{"role": "boot", "part": None}]
    for p in lay.parts:
        if p.fstype:
            items.append({"role": "part", "part": p.number, "method": "chunks", "mapped": True,
                          "stream_bytes": (200 * MIB if p.number == 3 else 20 * MIB)})
        else:
            items.append({"role": "part", "part": p.number, "method": "chunks", "mapped": False,
                          "stream_bytes": p.size})
    target = 900 * MIB
    sp = E.shrink_plan(lay, items, target)
    check(sp.part == 3, f"the big Windows partition is chosen ({sp.part}, {E.human(sp.old_size)} -> "
                        f"{E.human(sp.new_size)})")
    end = max(st + sz for st, sz in sp.new_parts.values())
    check(end + 34 * 512 <= target, f"new layout fits ({E.human(end)} of {E.human(target)})")
    check(sp.new_parts[4][1] == lay.parts[3].size and sp.new_parts[4][0] == lay.parts[3].start - sp.shift,
          "recovery partition keeps its size and moves forward")
    check(all(v[0] % MIB == 0 for v in sp.new_parts.values()), "all partitions stay 1 MiB aligned")
    # too small: refuse with a reason
    try:
        E.shrink_plan(lay, items, 600 * MIB)
        check(False, "a target that is too small for the data must be refused")
    except ValueError as e:
        check("no partition has enough free space" in str(e), f"too small refused: {e}")
    # partition table: apply the adjusted dump to a smaller image file with the real sfdisk
    tgt = w / "tgt.img"
    with open(tgt, "wb") as f:
        f.truncate(target)
    dump = E.adjust_sfdisk_dump(lay.sfdisk_dump, sp.new_parts, 512)
    r = sh("sfdisk", "-q", "--wipe", "never", str(tgt), inp=dump)
    check(r.returncode == 0, f"sfdisk accepts the new layout {r.stderr.strip()[:200]}")
    got = layout_of(tgt)
    check({p.number: [p.start, p.size] for p in got.parts} == sp.new_parts,
          "partition table on the smaller disk = planned layout")
    old = json.loads(sh("sfdisk", "-J", str(src)).stdout)["partitiontable"]["partitions"]
    new = json.loads(sh("sfdisk", "-J", str(tgt)).stdout)["partitiontable"]["partitions"]
    check([(p["type"], p.get("uuid"), p.get("name")) for p in old] ==
          [(p["type"], p.get("uuid"), p.get("name")) for p in new], "types, partition GUIDs and names kept")
    check(sh("sfdisk", "--verify", str(tgt)).returncode == 0, "sfdisk --verify ok")
    v = sh("sgdisk", "-v", str(tgt))
    check("No problems found" in v.stdout, "sgdisk -v: no problems (backup GPT at the new end)")
    # ext4 shrinking (resize2fs) on an image file, then check
    fsimg = w / "ext4.img"
    content = w / "content"
    content.mkdir()
    (content / "data.bin").write_bytes(b"x" * (30 * MIB))
    with open(fsimg, "wb") as f:
        f.truncate(256 * MIB)
    sh("mkfs.ext4", "-q", "-F", "-d", str(content), str(fsimg))
    E.shrink_filesystem("ext4", str(fsimg), 128 * MIB, print)
    blocks = int(sh("dumpe2fs", "-h", str(fsimg)).stdout.split("Block count:")[1].split()[0])
    bsize = int(sh("dumpe2fs", "-h", str(fsimg)).stdout.split("Block size:")[1].split()[0])
    check(blocks * bsize <= 128 * MIB, f"ext4 shrunk to {E.human(blocks * bsize)}")
    with open(fsimg, "r+b") as f:
        f.truncate(128 * MIB)               # cut off the rest, like the smaller partition
    check(E.check_filesystem("ext4", str(fsimg)) is None, "ext4 checks clean after cutting off the rest")
    out = sh("debugfs", "-R", "stat /data.bin", str(fsimg)).stdout
    check("Size: 31457280" in out, "file still there with its size")
    print("\nSHRINK TESTS " + ("PASSED" if not fails else f"{fails} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
