# SPDX-License-Identifier: GPL-3.0-only
"""diskimg engine: devices, image formats, network shares, and the job runner.
Everything here is independent of the user interface (text UI or GTK)."""
from __future__ import annotations

import errno
import fcntl
import glob as globmod
import hashlib
import json
import os
import pty
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.text import Text

VERSION = "4.1.0"
STATE_DIR = Path("/var/lib/diskimg")
MAP_DIR = STATE_DIR / "maps"
LOG_DIR = Path("/var/log/diskimg")
CONSOLE = os.environ.get("TERM", "") == "linux"   # bare Linux VT: limited glyphs
BORDER = "solid" if CONSOLE else "round"
SEL = ">" if CONSOLE else "●"
FAT_LIMIT = 4 * 1024**3 - 1
CHUNK = 1 << 20
MIB = 1 << 20
CONTAINER_EXT = ".dimg"
MANIFEST = "manifest.json"
NET_ROOT = Path("/media")                   # file managers list mounts here next to the drives
OLD_NET_ROOT = Path("/mnt/diskimg-net")     # until 4.1: saved repository paths may point here
BROWSE_ROOT = Path("/mnt/diskimg-browse")
CONF_DIR = Path("/etc/diskimg")
SHARES_FILE = CONF_DIR / "shares.json"
BROWSE_CACHE = STATE_DIR / "browse-cache"     # root-only; never a world-writable place like /var/tmp

MODES = {
    "d2i": ("Disk  ->  Image", "Back up a disk or partition into an image"),
    "d2d": ("Disk  ->  Disk", "Clone a disk or partition directly onto another"),
    "i2d": ("Image  ->  Disk", "Restore an image onto a disk or partition"),
    "chk": ("Check image", "Verify an existing backup. Nothing is written."),
    "brw": ("Browse backup", "Open partitions of a backup read-only to look at or copy files"),
    "fbk": ("File backup", "Back up folders (restic); browse every version instantly"),
}
PKG_ROOT = Path(__file__).resolve().parent.parent      # contains diskimg/ and libexec/
RUN_SCRIPT = PKG_ROOT / "libexec" / "diskimg-run"      # python entry used for sub-processes


class CondFlag:
    """Event-like object whose is_set() evaluates a condition."""

    def __init__(self, fn):
        self.fn = fn

    def is_set(self) -> bool:
        return bool(self.fn())

COMPRESSION = [("Off", 0), ("zstd fast (1)", 1), ("zstd balanced (3)", 3),
               ("zstd strong (9)", 9), ("zstd max (19, slow)", 19)]

PC_MAP = {
    "ntfs": ["partclone.ntfs"],
    "ext2": ["partclone.ext2", "partclone.extfs"],
    "ext3": ["partclone.ext3", "partclone.extfs"],
    "ext4": ["partclone.ext4", "partclone.extfs"],
    "vfat": ["partclone.fat", "partclone.vfat"],
    "exfat": ["partclone.exfat"],
    "xfs": ["partclone.xfs"],
    "btrfs": ["partclone.btrfs"],
    "f2fs": ["partclone.f2fs"],
    "hfsplus": ["partclone.hfsp"],
}
DOS_CONTAINER_TYPES = {"5", "f", "85"}

_proc_lock = threading.Lock()
_running_procs: set[subprocess.Popen] = set()


def _reg(p: subprocess.Popen) -> None:
    with _proc_lock:
        _running_procs.add(p)


def _unreg(p: subprocess.Popen) -> None:
    with _proc_lock:
        _running_procs.discard(p)


# ----------------------------------------------------------------------------
#  Helpers
# ----------------------------------------------------------------------------
def human(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


class RateMeter:
    """Transfer rate over the last few seconds (drops while a write blocks, unlike an average)."""

    def __init__(self, window: float = 5.0):
        self.window = window
        self.samples: deque = deque()

    def rate(self, total: int) -> float:
        now = time.monotonic()
        self.samples.append((now, total))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        t0, b0 = self.samples[0]
        return max(0.0, (total - b0) / (now - t0)) if now - t0 >= 0.5 else 0.0


def ratio_text(data: int, stored: int) -> str:
    """'2.4 : 1  (58 % saved)' for data bytes that took stored bytes."""
    if data <= 0 or stored <= 0:
        return "-"
    saved = (1 - stored / data) * 100
    return f"{data / stored:.1f} : 1  ({saved:.0f} % saved)" if saved >= 0 else \
        f"none ({-saved:.0f} % larger)"


def fmt_dur(s: float | None) -> str:
    if s is None or s < 0 or s > 86400 * 30:
        return "-"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def sanitize(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip())
    return s.strip("_") or "disk"


_STYLE_VAR = re.compile(r"\$(error|warning|success|accent)\b")


def plain_style_vars(s: str) -> str:
    """'[b $error]' -> '[b error]': Textual understands $variables in markup, plain Rich does not."""
    return _STYLE_VAR.sub(r"\1", s)


def strip_markup(s: str) -> str:
    try:
        return Text.from_markup(plain_style_vars(s)).plain
    except Exception:
        return s


def run(cmd: list[str], timeout: int | None = 60, inp: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp)


def excl_busy(path: str) -> bool:
    """True if the kernel refuses an exclusive open (mounted, swap, dm/md holder, locked)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
    except OSError as e:
        return e.errno == errno.EBUSY
    os.close(fd)
    return False


def fstype_of(path: Path) -> str:
    try:
        r = run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)], timeout=10)
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else "?"
    except Exception:
        return "?"


def net_share_of(path) -> str | None:
    """'SMB //server/share' / 'NFS server:/export' when path is on a network share, else None."""
    try:
        r = run(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "-T", str(path)], timeout=10)
        src, fst = r.stdout.strip().splitlines()[0].rsplit(None, 1)
    except Exception:
        return None
    if fst in ("cifs", "smb3"):
        return f"SMB {src.strip()}"
    if fst.startswith("nfs"):
        return f"NFS {src.strip()}"
    return None


def location_text(path) -> str:
    """'filesystem: ext4' or a clearly marked network share (Rich markup)."""
    net = net_share_of(path)
    if net:
        return f"[b $accent]NAS[/] network share {escape(net)}"
    return f"filesystem: {fstype_of(Path(path))}"


def has_gpt(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4096 + 8)
    except OSError:
        return False
    return head[512:520] == b"EFI PART" or head[4096:4104] == b"EFI PART"


def dev_size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def sector_size(path: str) -> int:
    try:
        return int(run(["blockdev", "--getss", path], timeout=10).stdout.strip())
    except Exception:
        return 512


def write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def pwrite_all(fd: int, data: bytes, off: int) -> None:
    mv = memoryview(data)
    while mv:
        n = os.pwrite(fd, mv, off)
        mv = mv[n:]
        off += n


def fsync_path(path: str) -> str | None:
    """fsync a file or device. Returns an error message, or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    return None


def drop_cache(path: str) -> str | None:
    """Write back and then forget every cached page of path, so that the following reads come
    from the medium itself (a verification must never be answered from RAM).
    Returns an error message, or None."""
    err = fsync_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            if stat.S_ISBLK(os.fstat(fd).st_mode):
                fcntl.ioctl(fd, 0x1261)        # BLKFLSBUF: flush and invalidate buffer cache
        finally:
            os.close(fd)
    except OSError as e:
        err = err or f"{path}: cannot drop cache ({e.strerror or e})"
    return err


def blkid_probe(node: str) -> dict[str, str]:
    """Low-level probe (no cache)."""
    try:
        r = run(["blkid", "-p", "-o", "export", node], timeout=60)
    except Exception:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        if k:
            out[k] = v
    return out


def pc_tool(fstype: str, allow_btrfs: bool) -> str | None:
    if fstype == "btrfs" and not allow_btrfs:
        return None
    for t in PC_MAP.get(fstype, []):
        if shutil.which(t):
            return t
    return None


def ntfs_backup_bootsector(node: str, part_size: int) -> tuple[int, int] | None:
    """Location of the NTFS backup boot sector (outside the cluster area, so
    partclone does not copy it). Returns (offset, length) or None."""
    try:
        fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC)
        try:
            bs = os.pread(fd, 512, 0)
        finally:
            os.close(fd)
    except OSError:
        return None
    if len(bs) < 512 or bs[3:11] != b"NTFS    ":
        return None
    bps = struct.unpack_from("<H", bs, 0x0B)[0]
    total = struct.unpack_from("<Q", bs, 0x28)[0]
    if bps not in (512, 1024, 2048, 4096):
        return None
    off = total * bps
    if off + bps > part_size:
        return None
    return off, bps


def ntfs_state(node: str) -> str:
    """Human readable NTFS warning, or ''. Strictly read-only."""
    if not shutil.which("ntfs-3g.probe"):
        return ""
    try:
        ro = run(["ntfs-3g.probe", "--readonly", node], timeout=60).returncode
    except Exception:
        return ""
    if ro == 13:
        return "NTFS is inconsistent (run chkdsk in Windows)"
    if ro == 14:
        return "Windows hibernated / Fast Startup active"
    if ro == 15:
        return "NTFS not cleanly unmounted"
    if ro != 0:
        return f"NTFS probe failed (code {ro})"
    if shutil.which("ntfscat"):
        try:
            p = subprocess.Popen(["ntfscat", node, "/hiberfil.sys"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            magic = p.stdout.read(4)
            p.kill()
            p.wait()
            if magic.lower() in (b"hibr", b"wake"):
                return "Windows hibernated / Fast Startup active"
        except OSError:
            pass
    return ""


class DevLock:
    """Exclusive open of a block device. While held, nothing can mount the
    device or any partition of it, but non-exclusive readers/writers work."""

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        if self.fd is None:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def released(self):
        held = self.fd is not None
        self.release()
        try:
            yield
        finally:
            if held:
                for _ in range(50):
                    try:
                        self.acquire()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    # never continue a job on a device that is no longer protected
                    raise OSError(errno.EBUSY, f"could not lock {self.path} again")


def kernel_parts(disk: str) -> dict[int, tuple[str, int, int]]:
    """number -> (node, start_bytes, size_bytes) as the kernel sees them."""
    name = Path(os.path.realpath(disk)).name
    base = Path("/sys/class/block") / name
    out: dict[int, tuple[str, int, int]] = {}
    try:
        for e in base.iterdir():
            pf = e / "partition"
            if pf.exists():
                try:
                    num = int(pf.read_text().strip())
                    start = int((e / "start").read_text().strip()) * 512
                    size = int((e / "size").read_text().strip()) * 512
                except (OSError, ValueError):
                    continue
                out[num] = (f"/dev/{e.name}", start, size)
    except OSError:
        pass
    return out


def reread_partitions(disk: str, expect: set[int], timeout: float = 15) -> dict[int, tuple[str, int, int]]:
    run(["partx", "-d", disk], timeout=30)
    run(["partx", "-a", disk], timeout=30)
    if shutil.which("udevadm"):
        run(["udevadm", "settle", "-t", "10"], timeout=20)
    t0 = time.monotonic()
    while True:
        parts = kernel_parts(disk)
        if expect.issubset(parts) and all(os.path.exists(parts[n][0]) for n in expect):
            return parts
        if time.monotonic() - t0 > timeout:
            return parts
        run(["partx", "-a", disk], timeout=30)
        time.sleep(0.5)


def zero_range(path: str, off: int, length: int) -> None:
    if length <= 0:
        return
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        z = b"\0" * min(length, MIB)
        end = off + length
        while off < end:
            n = min(len(z), end - off)
            pwrite_all(fd, z[:n], off)
            off += n
        os.fsync(fd)
    finally:
        os.close(fd)


# ----------------------------------------------------------------------------
#  Block devices
# ----------------------------------------------------------------------------
@dataclass
class Dev:
    path: str
    name: str
    kind: str
    size: int
    model: str
    serial: str
    tran: str
    rm: bool
    ro: bool
    fstype: str
    label: str
    pttype: str
    mounts: list[str]
    depth: int
    uuid: str = ""
    parent: "Dev | None" = None
    children: list["Dev"] = field(default_factory=list)
    busy: str = ""

    @property
    def desc(self) -> str:
        bits = [self.model or self.label or self.fstype or ""]
        if self.kind == "disk" and self.serial:
            bits.append(f"SN {self.serial}")
        return "  ".join(b for b in bits if b) or "-"

    @property
    def long(self) -> str:
        return f"{self.path}  {human(self.size)}  {self.desc}"

    @property
    def is_whole(self) -> bool:
        return self.kind != "part"

    def subtree(self):
        yield self
        for c in self.children:
            yield from c.subtree()

    def ancestors(self):
        p = self.parent
        while p:
            yield p
            p = p.parent

    @property
    def ident(self) -> str:
        serial = self.serial or next((a.serial for a in self.ancestors() if a.serial), "")
        base = sanitize(serial) if serial else "noserial"
        suffix = "" if (self.kind == "disk" and self.serial) else f"_{self.name}"
        return f"{base}_{self.size}{suffix}"


def _bool(v) -> bool:
    return v in (True, 1, "1", "true")


def load_devices(probe_excl: bool = True) -> dict[str, Dev]:
    """probe_excl=False skips the exclusive-open test (used by the periodic poll, so that other
    programs never see a short 'device busy' caused by diskimg)."""
    r = run(["lsblk", "-J", "-b", "-o",
             "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,FSTYPE,LABEL,PTTYPE,UUID,MOUNTPOINTS"], timeout=20)
    data = json.loads(r.stdout or '{"blockdevices": []}')
    out: dict[str, Dev] = {}

    def walk(node, depth, parent):
        mps = [m for m in (node.get("mountpoints") or []) if m]
        if not mps and node.get("mountpoint"):
            mps = [node["mountpoint"]]
        d = Dev(
            path=node.get("path") or f"/dev/{node['name']}",
            name=node["name"],
            kind=node.get("type") or "?",
            size=int(node.get("size") or 0),
            model=(node.get("model") or "").strip(),
            serial=(node.get("serial") or "").strip(),
            tran=node.get("tran") or "",
            rm=_bool(node.get("rm")),
            ro=_bool(node.get("ro")),
            fstype=node.get("fstype") or "",
            label=node.get("label") or "",
            pttype=node.get("pttype") or "",
            mounts=mps,
            depth=depth,
            uuid=node.get("uuid") or "",
            parent=parent,
        )
        if d.path not in out:
            out[d.path] = d
        for c in node.get("children") or []:
            d.children.append(walk(c, depth + 1, d))
        return d

    for n in data.get("blockdevices", []):
        walk(n, 0, None)

    for d in out.values():
        mounted = [m for x in d.subtree() for m in x.mounts]
        holders = sorted({x.kind for x in d.subtree() if x is not d and x.kind not in ("part", "disk")})
        if mounted:
            d.busy = "mounted at " + ", ".join(mounted[:3])
        elif holders:
            d.busy = "in use by " + "/".join(holders)
        elif probe_excl and d.kind in ("disk", "part", "loop") and d.size and excl_busy(d.path):
            d.busy = "busy (held by another process)"
    return out


def selectable(d: Dev) -> bool:
    if d.kind not in ("disk", "part", "loop"):
        return False
    if d.name.startswith(("zram", "ram")) or d.size == 0:
        return False
    return True


# ----------------------------------------------------------------------------
#  ddrescue mapfile
# ----------------------------------------------------------------------------
def parse_mapfile(path: Path) -> list[tuple[int, int, str]]:
    blocks: list[tuple[int, int, str]] = []
    seen_status = False
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if not seen_status:
                seen_status = True
                continue
            p = s.split()
            if len(p) >= 3:
                blocks.append((int(p[0], 0), int(p[1], 0), p[2]))
    return blocks


def map_totals(blocks) -> dict[str, int]:
    t: dict[str, int] = {}
    for _, sz, st in blocks:
        t[st] = t.get(st, 0) + sz
    return t


def image_unreadable_bytes(img: str) -> int:
    """Bytes recorded as not rescued in the ddrescue mapfile next to a raw image (0 if none)."""
    mp = Path(img + ".map")
    if not mp.exists():
        return 0
    try:
        return sum(v for k, v in map_totals(parse_mapfile(mp)).items() if k != "+")
    except (OSError, ValueError):
        return 0


def rescued_regions(blocks) -> list[tuple[int, int]]:
    regs: list[tuple[int, int]] = []
    for pos, sz, st in blocks:
        if st != "+" or sz == 0:
            continue
        if regs and regs[-1][0] + regs[-1][1] == pos:
            regs[-1] = (regs[-1][0], regs[-1][1] + sz)
        else:
            regs.append((pos, sz))
    return regs


# ----------------------------------------------------------------------------
#  Source layout analysis
# ----------------------------------------------------------------------------
@dataclass
class PartInfo:
    number: int | None
    node: str
    start: int            # byte offset on the source disk (0 for a single device)
    size: int             # bytes
    parttype: str = ""
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    method: str = "raw"   # partclone | raw | swap | skip
    tool: str = ""
    note: str = ""
    ntfs_bak: list[int] | None = None   # [offset, length] relative to partition
    used: int | None = None             # bytes in use (filesystem), if known

    @property
    def tag(self) -> str:
        return f"p{self.number}" if self.number is not None else "part"

    @staticmethod
    def from_json(d: dict) -> "PartInfo":
        keys = PartInfo.__dataclass_fields__.keys()
        return PartInfo(**{k: v for k, v in d.items() if k in keys})


@dataclass
class Layout:
    kind: str                 # disk (with partition table) | single (one device area)
    label: str | None         # gpt | dos | None
    sector_size: int
    size: int                 # size of the source device
    parts: list[PartInfo]
    boot_len: int = 0         # bytes before the first partition (MBR/GPT, boot code)
    needed: int = 0           # minimum target size
    sfdisk_dump: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("sfdisk_dump")
        return d

    @staticmethod
    def from_json(d: dict) -> "Layout":
        return Layout(kind=d["kind"], label=d.get("label"), sector_size=d.get("sector_size", 512),
                      size=d["size"], parts=[PartInfo.from_json(p) for p in d.get("parts", [])],
                      boot_len=d.get("boot_len", 0), needed=d.get("needed", d["size"]),
                      warnings=d.get("warnings", []))


def _decide(p: PartInfo, allow_btrfs: bool) -> None:
    if p.fstype == "swap":
        p.method, p.tool = "swap", "mkswap"
        return
    tool = pc_tool(p.fstype, allow_btrfs)
    if tool:
        p.method, p.tool = "partclone", tool
    else:
        p.method, p.tool = "raw", ""
        if p.fstype == "btrfs" and not allow_btrfs:
            p.note = "btrfs copied raw (partclone for btrfs disabled)"
        elif p.fstype:
            p.note = f"{p.fstype}: no filesystem-aware support, copied raw"
        else:
            p.note = "no known filesystem, copied raw"


def _probe_part(p: PartInfo, allow_btrfs: bool, warnings: list[str]) -> None:
    info = blkid_probe(p.node)
    p.fstype = info.get("TYPE", "")
    p.label = info.get("LABEL", "")
    p.uuid = info.get("UUID", "")
    _decide(p, allow_btrfs)
    if p.fstype == "ntfs":
        st = ntfs_state(p.node)
        if st:
            p.note = st
            warnings.append(f"{p.node}: {st}")
        if p.method == "partclone":
            loc = ntfs_backup_bootsector(p.node, p.size)
            p.ntfs_bak = list(loc) if loc else None


def analyze(dev: Dev, method: str, allow_btrfs: bool) -> Layout:
    ss = sector_size(dev.path)
    size = dev_size(dev.path)
    warnings: list[str] = []
    if method == "raw":
        p = PartInfo(None, dev.path, 0, size, method="raw", fstype=dev.fstype)
        return Layout("single", None, ss, size, [p], needed=size)

    if dev.kind == "part":
        p = PartInfo(None, dev.path, 0, size)
        _probe_part(p, allow_btrfs, warnings)
        return Layout("single", None, ss, size, [p], needed=size, warnings=warnings)

    whole = blkid_probe(dev.path)
    if whole.get("TYPE") and whole.get("TYPE") not in ("", "linux_raid_member", "LVM2_member",
                                                        "crypto_LUKS", "zfs_member", "BitLocker"):
        # filesystem directly on the whole device (no partition table)
        p = PartInfo(None, dev.path, 0, size)
        _probe_part(p, allow_btrfs, warnings)
        return Layout("single", None, ss, size, [p], needed=size, warnings=warnings)

    r = run(["sfdisk", "-J", dev.path], timeout=60)
    empty = False
    if r.returncode == 0 and r.stdout.strip():
        try:
            empty = not json.loads(r.stdout)["partitiontable"].get("partitions")
        except (ValueError, KeyError):
            empty = True
    if r.returncode != 0 or not r.stdout.strip() or empty:
        p = PartInfo(None, dev.path, 0, size)
        _probe_part(p, allow_btrfs, warnings)
        if not p.fstype:
            raise ValueError(f"{dev.path} has no partition table and no known filesystem. "
                             "Use the Raw method.")
        return Layout("single", None, ss, size, [p], needed=size, warnings=warnings)

    pt = json.loads(r.stdout)["partitiontable"]
    label = pt.get("label")
    if label not in ("gpt", "dos"):
        raise ValueError(f"Partition table type '{label}' is not supported. Use the Raw method.")
    ss = int(pt.get("sectorsize", ss))
    kparts = kernel_parts(dev.path)
    parts: list[PartInfo] = []
    for e in pt.get("partitions", []):
        m = re.search(r"(\d+)$", e["node"])
        if not m:
            raise ValueError(f"Cannot parse partition node {e['node']}")
        num = int(m.group(1))
        start = int(e["start"]) * ss
        psize = int(e["size"]) * ss
        ptype = str(e.get("type", "")).lower()
        node = kparts.get(num, (e["node"], 0, 0))[0]
        p = PartInfo(num, node, start, psize, parttype=ptype)
        if label == "dos" and ptype in DOS_CONTAINER_TYPES:
            p.method, p.note = "skip", "extended partition (container)"
        else:
            if num not in kparts:
                raise ValueError(f"The kernel does not know partition {num} of {dev.path}. "
                                 "Re-read the partition table (partprobe) and retry.")
            kn, kstart, ksize = kparts[num]
            if kstart != start or ksize != psize:
                raise ValueError(f"Kernel view of {kn} differs from the partition table. "
                                 "Re-read the partition table and retry.")
            _probe_part(p, allow_btrfs, warnings)
        parts.append(p)
    if not parts:
        raise ValueError(f"{dev.path} has an empty partition table. Use the Raw method.")
    first = min(p.start for p in parts)
    boot_len = first if first <= 256 * MIB else MIB
    if first > 256 * MIB:
        warnings.append("More than 256 MiB before the first partition. Only the first MiB is saved.")
    end = max(p.start + p.size for p in parts)
    needed = end + (34 * ss if label == "gpt" else 0)
    dump = run(["sfdisk", "-d", dev.path], timeout=60).stdout
    return Layout("disk", label, ss, size, parts, boot_len=boot_len, needed=needed,
                  sfdisk_dump=dump, warnings=warnings)


def layout_lines(lay: Layout, width: int = 96) -> list[str]:
    out = []
    if lay.kind == "disk":
        out.append(f"Partition table: {lay.label.upper() if lay.label else '-'}   "
                   f"sector size {lay.sector_size}   boot area {human(lay.boot_len)}")
    out.append(f"{'#':>3}  {'Start':>9}  {'Size':>9}  {'Used':>15}  {'FS':<10} {'Method':<16} Note")
    for p in lay.parts:
        meth = p.tool if p.method == "partclone" else p.method
        num = str(p.number) if p.number is not None else "-"
        used = (f"{human(p.used)} {p.used * 100 / max(p.size, 1):3.0f}%" if p.used is not None else "-")
        out.append(f"{num:>3}  {human(p.start):>9}  {human(p.size):>9}  {used:>15}  "
                   f"{(p.fstype or '-'):<10} {meth:<16} {p.note}"[:width])
    return out


# ----------------------------------------------------------------------------
#  Container (.dimg) format
# ----------------------------------------------------------------------------
def load_manifest(d: Path) -> dict:
    with open(d / MANIFEST) as f:
        man = json.load(f)
    if man.get("format") != "diskimg-container":
        raise ValueError("not a diskimg container")
    if int(man.get("version", 0)) > 3:
        raise ValueError(f"container version {man.get('version')} is newer than this diskimg")
    return man


def save_manifest(d: Path, man: dict) -> None:
    tmp = d / (MANIFEST + ".tmp")
    with open(tmp, "w") as f:
        json.dump(man, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, d / MANIFEST)
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


def is_container(p: Path) -> bool:
    return p.is_dir() and (p / MANIFEST).is_file()


def container_summary(man: dict) -> str:
    lay = man.get("layout", {})
    src = man.get("source", {})
    bits = ["filesystem-aware" if man.get("type") == "fs" else "raw (bit-exact)"]
    comp = man.get("compression")
    bits.append(f"zstd -{comp['level']}" if comp else "uncompressed")
    if man.get("chunked"):
        bits.append("[$success]instant browse[/]")
    bits.append("[b]encrypted[/b]" if man.get("encryption") else "not encrypted")
    if not man.get("complete"):
        bits.append("[b $error]INCOMPLETE[/]")
    elif man.get("verified"):
        bits.append("[$success]verified[/]")
    parts = [p for p in lay.get("parts", []) if p.get("method") != "skip"]
    return ("   ".join(bits) + f"\nfrom {escape(src.get('model') or src.get('path', '?'))} "
            f"{human(src.get('size'))}   {len(parts)} part(s)   needs >= {human(lay.get('needed'))}   "
            f"created {man.get('created', '?')[:16]}")


def remove_container(p: Path) -> None:
    if p.is_dir():
        if not (p / MANIFEST).exists() and any(p.iterdir()):
            raise ValueError(f"{p} is a non-empty folder that is not a diskimg backup. Refusing to delete it.")
        shutil.rmtree(p)
    elif p.exists():
        raise ValueError(f"{p} exists and is not a folder.")


# ----------------------------------------------------------------------------
#  Network shares (SMB / NFS)
# ----------------------------------------------------------------------------
NET_URL = re.compile(r"^(smb|cifs|nfs)://", re.I)
NET_FSTYPES = "cifs,smb3,nfs,nfs4"
SMB_VERSIONS = [("SMB auto", "auto"), ("SMB 3.1.1", "3.1.1"), ("SMB 3.0", "3.0"), ("SMB 2.1", "2.1"),
                ("SMB 2.0", "2.0"), ("SMB 1.0 (old NAS)", "1.0")]
MOUNT_HINTS = [
    ("error(13)", "Access denied: wrong user name or password, or no permission for this share."),
    ("error(2)", "Share or path not found on the server (check the share name)."),
    ("error(95)", "SMB version not supported by the server. Try another SMB version "
                  "(1.0 for very old NAS)."),
    ("error(112)", "Server not reachable (host is down)."),
    ("error(113)", "Server not reachable (no route to host)."),
    ("error(115)", "Connection timed out: server not reachable."),
    ("error(110)", "Connection timed out: server not reachable."),
    ("could not resolve address", "Server name could not be resolved. Use the IP address instead."),
    ("access denied by server", "NFS: access denied by the server (check the export's allowed hosts)."),
    ("connection refused", "The server refused the connection (is SMB/NFS enabled on the NAS?)."),
    ("no such file or directory", "Path not found on the server."),
]


def is_net_url(s: str) -> bool:
    s = s.strip()
    return bool(NET_URL.match(s)) or s.startswith("//") or s.startswith("\\\\")


def parse_net_url(s: str) -> tuple[dict, str]:
    """smb://[domain;]user[:pw]@server/share/sub/path, //server/share/..., \\\\server\\share\\...,
    nfs://server/export/path. Returns (connection, path inside the share)."""
    s = s.strip().replace("\\", "/")
    m = re.match(r"^(smb|cifs|nfs)://(.*)$", s, re.I)
    if m:
        proto = "nfs" if m.group(1).lower() == "nfs" else "smb"
        rest = m.group(2)
    elif s.startswith("//"):
        proto, rest = "smb", s[2:]
    else:
        raise ValueError("not a network path")
    host, _, path = rest.partition("/")
    user = password = domain = ""
    if "@" in host:
        user, host = host.rsplit("@", 1)
        user = urllib.parse.unquote(user)
        if ":" in user:
            user, password = user.split(":", 1)
        if ";" in user:
            domain, user = user.split(";", 1)
    if not host:
        raise ValueError("The network path has no server name.")
    path = urllib.parse.unquote(path).strip("/")
    if proto == "smb":
        share, _, sub = path.partition("/")
        if not share:
            raise ValueError("The network path has no share name (smb://server/SHARE/...).")
    else:
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError("The NFS path has no export path (nfs://server/export/...).")
        if len(parts) == 1:
            share, sub = "/" + parts[0], ""
        else:
            share, sub = "/" + "/".join(parts[:-1]), parts[-1]
    conn = {"proto": proto, "server": host, "share": share, "user": user, "domain": domain,
            "vers": "auto"}
    if password:
        conn["password"] = password
    return conn, sub


def conn_source(c: dict) -> str:
    return f"//{c['server']}/{c['share'].strip('/')}" if c["proto"] == "smb" else f"{c['server']}:{c['share']}"


def conn_key(c: dict) -> str:
    return f"{c['proto']}://{c['server'].lower()}/{c['share'].strip('/')}"


def net_mounts() -> list[dict]:
    try:
        r = run(["findmnt", "-J", "-t", NET_FSTYPES, "-o", "SOURCE,TARGET,FSTYPE"], timeout=10)
        return json.loads(r.stdout).get("filesystems", []) if r.stdout.strip() else []
    except Exception:
        return []


def find_mounted(c: dict) -> str | None:
    want = conn_source(c).lower().rstrip("/")
    for m in net_mounts():
        if (m.get("source") or "").lower().rstrip("/") == want:
            return m.get("target")
    return None


def load_shares() -> list[dict]:
    try:
        data = json.loads(SHARES_FILE.read_text())
        return [d for d in data if isinstance(d, dict) and d.get("server") and d.get("share")]
    except (OSError, ValueError):
        return []


def save_shares(items: list[dict]) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    tmp = SHARES_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, SHARES_FILE)


def explain_mount_error(text: str) -> str:
    low = text.lower()
    first = (text.strip().splitlines() or [""])[0][:160]
    for k, msg in MOUNT_HINTS:
        if k in low:
            return f"{msg}  ({first})"
    return text.strip()[:300] or "mount failed"


def mount_share(conn: dict, password: str) -> str:
    """Mount an SMB/NFS share (or reuse an existing mount). Returns the local path."""
    existing = find_mounted(conn)
    if existing:
        return existing
    # "NAS server share" under /media: Nautilus & co. show it with the drives ("This PC" / Other
    # Locations), with a network icon and a name that says what it is
    base = NET_ROOT / f"NAS {sanitize(conn['server'])} {sanitize(conn['share'])}"
    mp, n = base, 2
    while os.path.ismount(mp):
        mp = Path(f"{base} {n}")
        n += 1
    NET_ROOT.mkdir(mode=0o755, exist_ok=True)
    mp.mkdir(parents=True, exist_ok=True)
    cred = None
    if conn["proto"] == "smb":
        if not shutil.which("mount.cifs"):
            raise ValueError("mount.cifs is missing. Install it: pacman -S cifs-utils")
        opts = ["iocharset=utf8"]
        if conn.get("user"):
            rd = Path("/run/diskimg")
            rd.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, cred = tempfile.mkstemp(dir=rd, prefix="cred-")
            with os.fdopen(fd, "w") as f:
                f.write(f"username={conn['user']}\npassword={password}\n")
                if conn.get("domain"):
                    f.write(f"domain={conn['domain']}\n")
            opts.append(f"credentials={cred}")
        else:
            opts.append("guest")
        if conn.get("vers") not in (None, "", "auto"):
            opts.append(f"vers={conn['vers']}")
        cmd = ["mount", "-t", "cifs", conn_source(conn), str(mp), "-o", ",".join(opts)]
    else:
        if not shutil.which("mount.nfs"):
            raise ValueError("mount.nfs is missing. Install it: pacman -S nfs-utils")
        cmd = ["mount", "-t", "nfs", conn_source(conn), str(mp), "-o", "hard"]
    try:
        r = run(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(cmd, 1, "", "error(110): timed out")
    finally:
        if cred:
            try:
                os.unlink(cred)
            except OSError:
                pass
    if r.returncode != 0:
        try:
            mp.rmdir()
        except OSError:
            pass
        raise ValueError(explain_mount_error((r.stderr or "") + (r.stdout or "")))
    # saved file backup sets may name the old place (/mnt/diskimg-net/server_share/...)
    old = OLD_NET_ROOT / sanitize(f"{conn['server']}_{conn['share']}")
    try:
        if old.is_dir() and not old.is_symlink() and not os.path.ismount(old):
            old.rmdir()             # an empty leftover folder (fails harmlessly if not empty)
        if old.is_symlink() or not old.exists():
            OLD_NET_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = old.with_name(old.name + ".new")
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(mp)
            os.replace(tmp, old)
    except OSError:
        pass
    return str(mp)


def umount_path(mp: str, lazy: bool = False) -> str | None:
    if os.path.ismount(mp):
        r = run(["umount", mp], timeout=60)
        if r.returncode != 0 and os.path.ismount(mp):
            if not lazy:
                return (r.stderr or r.stdout).strip() or "umount failed"
            r = run(["umount", "-l", mp], timeout=60)
            if r.returncode != 0 and os.path.ismount(mp):
                return (r.stderr or r.stdout).strip() or "umount failed"
    try:
        os.rmdir(mp)
    except OSError:
        pass
    return None


def places_options(extra: list[str] = ()) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("/", "/")]
    seen = {"/"}

    def add(label: str, path: str, check: bool = True) -> None:
        if path and path not in seen and (not check or os.path.isdir(path)):
            seen.add(path)
            out.append((label, path))
    for p in extra:
        add(p, p)
    for m in net_mounts():
        # listed by findmnt, so it exists; stat()ing an unreachable NAS could hang the UI
        add(f"NAS  {m.get('source')}", m.get("target") or "", check=False)
    for p in sorted(globmod.glob("/run/media/*/*")):
        add(p, p)
    for p in ("/mnt", "/media", "/home", "/var/tmp", "/root"):
        add(p, p)
    return out


def existing_ancestor(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


# ----------------------------------------------------------------------------
#  Browsing backups (unpack + read-only mount)
# ----------------------------------------------------------------------------
MOUNTABLE = {"ntfs", "vfat", "exfat", "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "hfsplus"}
NO_CACHE_FS = {"vfat", "msdos", "fat", "exfat", "tmpfs", "overlay", "ramfs", "squashfs", "iso9660"}


def desktop_user() -> tuple[str, int, int] | None:
    """The non-root desktop user who started diskimg (via sudo / pkexec / active session)."""
    name = os.environ.get("SUDO_USER") or ""
    if name and name != "root":
        try:
            pw = pwd.getpwnam(name)
            return pw.pw_name, pw.pw_uid, pw.pw_gid
        except KeyError:
            pass
    if os.environ.get("PKEXEC_UID", "").isdigit():
        try:
            pw = pwd.getpwuid(int(os.environ["PKEXEC_UID"]))
            if pw.pw_uid != 0:
                return pw.pw_name, pw.pw_uid, pw.pw_gid
        except KeyError:
            pass
    if shutil.which("loginctl"):
        try:
            r = run(["loginctl", "list-sessions", "--no-legend"], timeout=10)
            for line in r.stdout.splitlines():
                if not line.split():
                    continue
                sid = line.split()[0]
                out = run(["loginctl", "show-session", sid, "-p", "Type", "-p", "Name", "-p", "Active"],
                          timeout=10).stdout
                d = dict(x.split("=", 1) for x in out.splitlines() if "=" in x)
                if d.get("Type") in ("wayland", "x11") and d.get("Active") == "yes" \
                        and d.get("Name") not in ("", "root"):
                    pw = pwd.getpwnam(d["Name"])
                    return pw.pw_name, pw.pw_uid, pw.pw_gid
        except Exception:
            pass
    return None


def open_in_file_manager(path: str) -> str | None:
    """Open path in the desktop user's file manager (Dolphin if present). Returns an error or None."""
    du = desktop_user()
    if not du:
        return ("No desktop user found. Start diskimg with sudo from a terminal inside your desktop "
                "session, or use 'Copy to...'.")
    user, uid, _gid = du
    exe = shutil.which("dolphin") or shutil.which("xdg-open")
    if not exe:
        return "No graphical file manager found (Dolphin / xdg-open). Use 'Copy to...'."
    args = [exe, "--new-window", path] if os.path.basename(exe) == "dolphin" else [exe, path]
    rt = f"/run/user/{uid}"
    if shutil.which("systemd-run") and os.path.exists(f"{rt}/bus"):
        r = run(["systemd-run", f"--machine={user}@", "--user", "--collect", "--quiet",
                 "--no-block"] + args, timeout=30)
        if r.returncode == 0:
            return None
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = "/"
    env = {"HOME": home, "USER": user, "LOGNAME": user, "PATH": "/usr/local/bin:/usr/bin:/bin",
           "XDG_RUNTIME_DIR": rt, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={rt}/bus",
           "DISPLAY": os.environ.get("DISPLAY") or ":0"}
    way = sorted(p for p in globmod.glob(f"{rt}/wayland-*") if not p.endswith(".lock"))
    if way:
        env["WAYLAND_DISPLAY"] = os.path.basename(way[0])
    try:
        subprocess.Popen(["runuser", "-u", user, "--", "env", "-i"]
                         + [f"{k}={v}" for k, v in env.items()] + args,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"Could not start the file manager: {e}"
    return None


OWNERLESS_FS = ("ntfs", "vfat", "exfat")     # mount options decide the owner of every file


def mount_ro(dev: str, fstype: str, mp: str) -> str | None:
    """Mount read-only (never device nodes or set-uid programs from a backup). Filesystems without
    Unix owners belong to the desktop user; the others keep their owners (see start_view).
    Returns an error message or None."""
    du = desktop_user()
    uid, gid = (du[1], du[2]) if du else (0, 0)
    own = f"nodev,nosuid,uid={uid},gid={gid},fmask=0133,dmask=0022"
    os.makedirs(mp, exist_ok=True)
    tries: list[list[str]] = []
    if fstype == "ntfs":
        # the kernel driver first: it reads files Windows compressed with CompactOS / WOF (many
        # files under Windows and Program Files), which ntfs-3g cannot read without an extra plugin
        tries.append(["mount", "-t", "ntfs3", "-o", f"ro,force,{own}", dev, mp])
        if shutil.which("ntfs-3g"):
            tries.append(["ntfs-3g", "-o", f"ro,{own}", dev, mp])
    elif fstype in ("vfat", "exfat"):
        tries.append(["mount", "-t", fstype, "-o", f"ro,{own}", dev, mp])
    elif fstype in ("ext2", "ext3", "ext4"):
        tries.append(["mount", "-t", fstype, "-o", "ro,noload,nodev,nosuid", dev, mp])
    elif fstype == "xfs":
        tries.append(["mount", "-t", "xfs", "-o", "ro,norecovery,nodev,nosuid", dev, mp])
    elif fstype == "btrfs":
        tries.append(["mount", "-t", "btrfs", "-o", "ro,rescue=nologreplay,nodev,nosuid", dev, mp])
        tries.append(["mount", "-t", "btrfs", "-o", "ro,nodev,nosuid", dev, mp])
    elif fstype:
        tries.append(["mount", "-t", fstype, "-o", "ro,nodev,nosuid", dev, mp])
    else:
        return "no filesystem found"
    errs = []
    for c in tries:
        r = run(c, timeout=120)
        if r.returncode == 0:
            return None
        errs.append(((r.stderr or r.stdout).strip().splitlines() or ["failed"])[-1])
    try:
        os.rmdir(mp)
    except OSError:
        pass
    return "; ".join(errs)


@dataclass
class BrowseSession:
    name: str
    source: str
    root: Path
    mounts: list[tuple[str, str]] = field(default_factory=list)    # (mount point, description)
    loops: list[str] = field(default_factory=list)
    caches: list[Path] = field(default_factory=list)
    keep: bool = False
    procs: list[subprocess.Popen] = field(default_factory=list)
    fuse_mounts: list[str] = field(default_factory=list)
    note: str = ""
    views: list[subprocess.Popen] = field(default_factory=list)    # serve-view processes (start_view)
    stages: list[str] = field(default_factory=list)    # root-only mounts behind the views


def close_browse(sess: BrowseSession, force: bool = False) -> list[str]:
    """Unmount, detach loops, delete unpacked files (unless kept). Returns errors."""
    errs: list[str] = []
    remaining = []
    for mp, desc in reversed(sess.mounts):
        e = umount_path(mp, lazy=force)
        if e:
            errs.append(f"{mp}: {e}")
            remaining.append((mp, desc))
    sess.mounts = list(reversed(remaining))
    if sess.mounts:
        return errs
    # the file views end when their mount is gone (a lazily detached one: when its last user lets go)
    for p in sess.views:
        try:
            p.wait(timeout=5 if force else 15)
        except subprocess.TimeoutExpired:
            if not force:
                p.kill()
                p.wait()
        if p.poll() is not None:
            _unreg(p)
    sess.views = [p for p in sess.views if p.poll() is None]
    remaining_s = []
    for st in reversed(sess.stages):
        e = umount_path(st, lazy=force or bool(sess.views))
        if e:
            errs.append(f"{st}: {e}")
            remaining_s.append(st)
    sess.stages = list(reversed(remaining_s))
    if sess.stages:
        return errs
    for lp in sess.loops:
        run(["losetup", "-d", lp], timeout=30)
    sess.loops = []
    remaining_f = []
    for fm in sess.fuse_mounts:
        if os.path.ismount(fm):
            r = run(["fusermount3", "-u"] + (["-z"] if force else []) + [fm], timeout=60)
            if r.returncode != 0 and os.path.ismount(fm):
                errs.append(f"{fm}: {(r.stderr or r.stdout).strip()}")
                remaining_f.append(fm)
                continue
        try:
            os.rmdir(fm)
        except OSError:
            pass
    sess.fuse_mounts = remaining_f
    if remaining_f:
        return errs
    for p in sess.procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        _unreg(p)
    sess.procs = []
    if not sess.keep:
        for c in sess.caches:
            try:
                c.unlink()
            except OSError:
                pass
        for d in {c.parent for c in sess.caches}:
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        sess.root.rmdir()
    except OSError:
        pass
    return errs


def browse_candidates(p: Path, man: dict | None) -> list[dict]:
    if man is None:
        return [{"id": "img", "label": "", "fs": "all partitions", "size": p.stat().st_size, "need": 0,
                 "ok": True, "max": False, "note": "opened directly, nothing to unpack"}]
    lay = Layout.from_json(man["layout"])
    parts = {pp.number: pp for pp in lay.parts}
    if man.get("type") != "fs":
        it = next((i for i in man["items"] if i.get("file") and i.get("role") == "part"), None)
        if not it:
            return []
        n = int(it.get("stream_bytes") or 0)
        if it.get("method") == "chunks":
            return [{"id": it["id"], "label": "", "fs": "all partitions", "size": n, "need": 0,
                     "ok": True, "max": False, "note": "opens instantly"}]
        return [{"id": it["id"], "label": "", "fs": "all partitions", "size": n, "need": n, "ok": True,
                 "max": True, "note": "unpacked as one sparse file (zeros take no space)"}]
    rows = []
    for it in man["items"]:
        if it.get("role") != "part" or it.get("method") not in ("partclone", "raw", "chunks") \
                or not it.get("file"):
            continue
        pp = parts.get(it.get("part"))
        fs = (pp.fstype if pp else it.get("fstype")) or ""
        ok = fs in MOUNTABLE
        if fs == "BitLocker":
            note = "BitLocker-encrypted: cannot be opened here"
        elif not ok:
            note = f"{fs}: cannot be opened here" if fs else "no known filesystem"
        else:
            note = "raw copy" if it["method"] == "raw" or (it["method"] == "chunks" and
                                                            not it.get("mapped")) else ""
        chunk = it["method"] == "chunks"
        if chunk and ok:
            note = (note + ", " if note else "") + "opens instantly"
        rows.append({"id": it["id"], "label": (pp.label if pp else "") or "", "fs": fs or "-",
                     "size": int(it.get("length") or 0),
                     "need": 0 if chunk else int(it.get("stream_bytes") or 0),
                     "ok": ok, "max": it["method"] == "raw", "note": note})
    return rows


def default_cache_dir(backup: Path, need: int) -> str:
    live = Path("/run/archiso").exists()
    cands = ([BROWSE_CACHE] if not live else []) + [backup.parent / "diskimg-browse"]
    for c in cands:
        base = existing_ancestor(c)
        try:
            free = shutil.disk_usage(base).free
        except OSError:
            continue
        if fstype_of(base) not in NO_CACHE_FS and free > need * 1.1 + 256 * MIB:
            return str(c)
    return str(cands[-1])


def trusted_uids() -> set[int]:
    """root and the desktop user who started diskimg (e.g. owner of an automounted USB disk)."""
    du = desktop_user()
    return {0} | ({du[1]} if du else set())


def secure_cache_dir(d: Path) -> None:
    """Create d (if needed) and make sure only root can put files into it. Root writes, reuses and
    loop-mounts the files in this folder, so another user must not be able to plant files or
    symlinks there."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{d} is not a folder (symbolic links are not accepted).")
    trusted = trusted_uids()
    if st.st_uid not in trusted:
        raise ValueError(f"{d} belongs to another user. Choose another folder for the unpacked data.")
    if st.st_mode & 0o022:
        try:
            os.chmod(d, stat.S_IMODE(st.st_mode) & ~0o022)
        except OSError:
            pass
        if os.lstat(d).st_mode & 0o022:
            raise ValueError(f"{d} is writable by other users. Choose another folder.")
    for parent in d.parents:
        try:
            pst = os.lstat(parent)
        except OSError:
            continue
        # a folder another user may rename/replace (writable, not sticky, not root's) is unsafe
        if pst.st_uid not in trusted and (pst.st_mode & 0o002) \
                and not (pst.st_mode & stat.S_ISVTX):
            raise ValueError(f"{parent} is writable by other users. Choose another folder.")


def unique_dest(dst_dir: Path, name: str) -> Path:
    d = dst_dir / name
    n = 1
    while d.exists() or d.is_symlink():
        d = dst_dir / f"{name} (copy {n})"
        n += 1
    return d


def tree_size(src: Path, cancel: threading.Event) -> tuple[int, int]:
    total = files = 0
    st = os.lstat(src)
    if not stat.S_ISDIR(st.st_mode):
        return (st.st_size if stat.S_ISREG(st.st_mode) else 0), 1
    for root, dirs, fnames in os.walk(src):
        if cancel.is_set():
            break
        for f in fnames:
            try:
                s = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            if stat.S_ISREG(s.st_mode):
                total += s.st_size
            files += 1
    return total, files


def copy_item(src: Path, dest: Path, state: dict, cancel: threading.Event, errors: list[str]) -> None:
    if cancel.is_set():
        return
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dest)
        state["files"] += 1
        return
    if stat.S_ISDIR(st.st_mode):
        os.mkdir(dest, 0o755)
        try:
            entries = sorted(os.listdir(src))
        except OSError as e:
            errors.append(f"{src}: {e.strerror}")
            entries = []
        for e in entries:
            if cancel.is_set():
                return
            try:
                copy_item(src / e, dest / e, state, cancel, errors)
            except OSError as ex:
                errors.append(f"{src / e}: {ex.strerror or ex}")
        try:
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
        return
    if stat.S_ISREG(st.st_mode):
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            while True:
                if cancel.is_set():
                    return
                b = fi.read(CHUNK)
                if not b:
                    break
                fo.write(b)
                state["bytes"] += len(b)
        try:
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
        state["files"] += 1
        return
    errors.append(f"{src}: special file skipped")


def chown_tree(path: Path, uid: int, gid: int) -> None:
    os.lchown(path, uid, gid)
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                try:
                    os.lchown(os.path.join(root, n), uid, gid)
                except OSError:
                    pass


# ----------------------------------------------------------------------------
#  Chunked format (container v3): independently compressed 4 MiB blocks + index.
#  Any byte range can be read without decompressing the rest ("instant browse").
# ----------------------------------------------------------------------------
CHUNK_SIZE = 4 * MIB
IDX_MAGIC = b"DIMGIDX1"
IDX_HDR = struct.Struct("<8sIQQ4x")        # magic, chunk size, total size, chunk count (32 bytes)
IDX_ENT = struct.Struct("<QIIB16s")        # data offset, stored size, plain size, flag, blake2b-128
F_ABSENT, F_ZSTD, F_ZERO, F_PLAIN = 0, 1, 2, 3
ZERO_CHUNK = bytes(CHUNK_SIZE)


class ChunkError(OSError):
    pass


class _ZCodec:
    """zstd via Python 3.14's compression.zstd, or the 'zstandard' module as fallback."""

    def __init__(self):
        self.kind = None
        try:
            from compression import zstd as z  # type: ignore
            self.z, self.kind = z, "stdlib"
        except ImportError:
            try:
                import zstandard as z  # type: ignore
                self.z, self.kind = z, "zstandard"
            except ImportError:
                self.z = None

    def compress(self, data: bytes, level: int) -> bytes:
        if self.kind == "stdlib":
            p = self.z.CompressionParameter
            return self.z.compress(data, options={p.compression_level: level, p.checksum_flag: 1})
        return self.z.ZstdCompressor(level=level, write_checksum=True,
                                     write_content_size=True).compress(data)

    def decompress(self, data: bytes, size: int) -> bytes:
        """Decompress one frame of exactly `size` bytes; never produces more than that (a damaged
        or crafted chunk cannot make diskimg allocate huge amounts of memory)."""
        if self.kind == "stdlib":
            d = self.z.ZstdDecompressor()
            out = d.decompress(data, max_length=size)
            if not d.eof or d.unused_data:
                raise ValueError("zstd frame does not match the recorded size")
            return out
        return self.z.ZstdDecompressor().decompress(data, max_output_size=size)


ZSTD = _ZCodec()


def chunk_hash(b: bytes) -> bytes:
    return hashlib.blake2b(b, digest_size=16).digest()


class ChunkIndex:
    def __init__(self, path: Path | str):
        with open(path, "rb") as f:
            self.buf = f.read()
        if len(self.buf) < IDX_HDR.size:
            raise ChunkError(errno.EIO, f"{path}: chunk index too short")
        magic, self.chunk, self.size, self.count = IDX_HDR.unpack_from(self.buf, 0)
        if magic != IDX_MAGIC:
            raise ChunkError(errno.EIO, f"{path}: not a diskimg chunk index")
        if len(self.buf) != IDX_HDR.size + self.count * IDX_ENT.size:
            raise ChunkError(errno.EIO, f"{path}: chunk index truncated")
        if not (4096 <= self.chunk <= 256 * MIB) or self.count != (self.size + self.chunk - 1) // self.chunk:
            raise ChunkError(errno.EIO, f"{path}: chunk index header is inconsistent")
        last = self.size - (self.count - 1) * self.chunk if self.count else 0
        for i in range(self.count):
            _off, csz, usz, flag, _h = IDX_ENT.unpack_from(self.buf, IDX_HDR.size + i * IDX_ENT.size)
            want = self.chunk if i < self.count - 1 else last
            if flag not in (F_ABSENT, F_ZSTD, F_ZERO, F_PLAIN) or usz != want \
                    or (flag == F_PLAIN and csz != usz) or (flag == F_ZSTD and csz == 0):
                raise ChunkError(errno.EIO, f"{path}: chunk index entry {i} is damaged")

    def entry(self, i: int) -> tuple[int, int, int, int, bytes]:
        return IDX_ENT.unpack_from(self.buf, IDX_HDR.size + i * IDX_ENT.size)

    def stats(self) -> dict:
        c = {F_ABSENT: 0, F_ZSTD: 0, F_ZERO: 0, F_PLAIN: 0}
        for i in range(self.count):
            c[self.entry(i)[3]] += 1
        return {"data": c[F_ZSTD] + c[F_PLAIN], "zero": c[F_ZERO], "absent": c[F_ABSENT]}


class ChunkReader:
    """Thread-safe random access to a chunked image, with an LRU cache. Every chunk is checked
    against its stored hash; damaged chunks raise ChunkError (EIO) instead of returning bad data."""

    def __init__(self, data_path: Path | str, index_path: Path | str, cache_chunks: int = 16):
        if ZSTD.kind is None:
            raise ChunkError(errno.ENOSYS, "no zstd module (Python 3.14+ or python-zstandard needed)")
        self.idx = ChunkIndex(index_path)
        self.fd = os.open(data_path, os.O_RDONLY | os.O_CLOEXEC)
        self.size, self.chunk = self.idx.size, self.idx.chunk
        self.cache: "OrderedDict[int, bytes]" = OrderedDict()
        self.lock = threading.Lock()
        self.max = cache_chunks

    def get(self, i: int) -> bytes:
        with self.lock:
            b = self.cache.get(i)
            if b is not None:
                self.cache.move_to_end(i)
                return b
        off, csz, usz, flag, h = self.idx.entry(i)
        if flag in (F_ABSENT, F_ZERO):
            return ZERO_CHUNK[:usz] if usz <= CHUNK_SIZE else bytes(usz)
        raw = os.pread(self.fd, csz, off)
        if len(raw) != csz:
            raise ChunkError(errno.EIO, f"chunk {i}: short read from backup file")
        try:
            data = ZSTD.decompress(raw, usz) if flag == F_ZSTD else raw
        except Exception as e:  # noqa: BLE001
            raise ChunkError(errno.EIO, f"chunk {i}: {e}")
        if len(data) != usz or chunk_hash(data) != h:
            raise ChunkError(errno.EIO, f"chunk {i}: checksum mismatch (backup damaged)")
        with self.lock:
            self.cache[i] = data
            while len(self.cache) > self.max:
                self.cache.popitem(last=False)
        return data

    def read(self, off: int, n: int) -> bytes:
        if off >= self.size or n <= 0:
            return b""
        n = min(n, self.size - off)
        parts = []
        while n > 0:
            i = off // self.chunk
            co = off - i * self.chunk
            b = self.get(i)
            take = min(n, len(b) - co)
            parts.append(b[co:co + take])
            off += take
            n -= take
        return b"".join(parts)

    def close(self) -> None:
        os.close(self.fd)


def merge_regions(regs: list[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for o, n in sorted((o, n) for o, n in regs if n > 0 and o < total):
        e = min(total, o + n)
        if out and o <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([o, e])
    return [(s, e - s) for s, e in out]


def rescue_read(fd: int, off: int, n: int) -> tuple[bytes, list[tuple[int, int]]]:
    """Read n bytes; unreadable 4 KiB blocks become zeros and are reported."""
    try:
        b = os.pread(fd, n, off)
        if len(b) == n:
            return b, []
    except OSError:
        pass
    out = bytearray()
    bad: list[tuple[int, int]] = []
    p, end = off, off + n
    while p < end:
        q = min(end, (p // 65536 + 1) * 65536)
        try:
            b = os.pread(fd, q - p, p)
            if len(b) != q - p:
                raise OSError(errno.EIO, "short read")
            out += b
        except OSError:
            s = p
            while s < q:
                t = min(q, (s // 4096 + 1) * 4096)
                try:
                    b = os.pread(fd, t - s, s)
                    if len(b) != t - s:
                        raise OSError(errno.EIO, "short read")
                    out += b
                except OSError:
                    out += bytes(t - s)
                    if bad and bad[-1][0] + bad[-1][1] == s:
                        bad[-1] = (bad[-1][0], bad[-1][1] + t - s)
                    else:
                        bad.append((s, t - s))
                s = t
        p = q
    return bytes(out), bad


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    s = max(a[0], b[0])
    e = min(a[0] + a[1], b[0] + b[1])
    return max(0, e - s)


def chunked_backup(src: str, regions: list[tuple[int, int]], total: int, level: int,
                   data_path: Path, index_path: Path, cancel: threading.Event,
                   progress=None) -> dict:
    """Read the used regions of src (a device or partition) and write a chunked image.
    Chunks without used data are not stored ("absent"); all-zero chunks are only flagged."""
    regions = merge_regions(regions, total)
    used_total = sum(n for _, n in regions)
    nchunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    workers = max(1, min(os.cpu_count() or 1, 8))
    fd = os.open(src, os.O_RDONLY | os.O_CLOEXEC)
    out = open(data_path, "wb", buffering=8 * MIB)
    sha = hashlib.sha256()
    entries = bytearray()
    pos = 0
    bad = 0
    bad_ranges: list[list[int]] = []
    done_used = 0
    pending: deque = deque()
    ri = 0
    last_drop = 0
    stats = {"data": 0, "zero": 0, "absent": 0}
    packed_in = 0           # used data in the chunks written so far (for the compression ratio)

    def compress(b: bytes) -> bytes:
        return ZSTD.compress(b, level)

    def flush_one() -> None:
        nonlocal pos, packed_in
        i, usz, fut, flag, h, plain, ub = pending.popleft()
        packed_in += ub
        if flag in (F_ABSENT, F_ZERO):
            entries.extend(IDX_ENT.pack(0, 0, usz, flag, h))
            return
        payload = plain
        if fut is not None:
            comp = fut.result()
            if len(comp) < usz:
                payload, flag = comp, F_ZSTD
            else:
                flag = F_PLAIN
        out.write(payload)
        sha.update(payload)
        entries.extend(IDX_ENT.pack(pos, len(payload), usz, flag, h))
        pos += len(payload)

    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(nchunks):
                if cancel.is_set():
                    raise InterruptedError("cancelled")
                cs = i * CHUNK_SIZE
                ce = min(total, cs + CHUNK_SIZE)
                usz = ce - cs
                while ri < len(regions) and regions[ri][0] + regions[ri][1] <= cs:
                    ri += 1
                ov = []
                k = ri
                while k < len(regions) and regions[k][0] < ce:
                    s = max(cs, regions[k][0])
                    e = min(ce, regions[k][0] + regions[k][1])
                    if e > s:
                        ov.append((s, e))
                    k += 1
                if not ov:
                    pending.append((i, usz, None, F_ABSENT, bytes(16), None, 0))
                    stats["absent"] += 1
                else:
                    span_s, span_e = ov[0][0], ov[-1][1]
                    data, badr = rescue_read(fd, span_s, span_e - span_s)
                    if len(ov) == 1 and span_s == cs and span_e == ce:
                        b = data
                    else:
                        buf = bytearray(usz)
                        for s, e in ov:
                            buf[s - cs:e - cs] = data[s - span_s:e - span_s]
                        b = bytes(buf)
                    for bo, bl in badr:
                        lost = sum(_overlap((bo, bl), (s, e - s)) for s, e in ov)
                        if lost:
                            bad += lost
                            if len(bad_ranges) < 1000:
                                bad_ranges.append([bo, bl])
                    ub = sum(e - s for s, e in ov)
                    done_used += ub
                    h = chunk_hash(b)
                    if b == ZERO_CHUNK[:usz]:
                        pending.append((i, usz, None, F_ZERO, h, None, ub))
                        stats["zero"] += 1
                    else:
                        fut = pool.submit(compress, b) if level else None
                        pending.append((i, usz, fut, F_PLAIN, h, b, ub))
                        stats["data"] += 1
                while len(pending) > 2 * workers:
                    flush_one()
                if done_used - last_drop > 256 * MIB:
                    last_drop = done_used
                    try:
                        os.posix_fadvise(fd, 0, ce, os.POSIX_FADV_DONTNEED)
                        out.flush()
                        os.posix_fadvise(out.fileno(), 0, pos, os.POSIX_FADV_DONTNEED)
                    except OSError:
                        pass
                if progress:
                    progress(done_used, used_total, bad, pos, packed_in)
            while pending:
                flush_one()
            if progress:
                progress(done_used, used_total, bad, pos, packed_in)
        out.flush()
        os.fsync(out.fileno())
        try:
            os.posix_fadvise(out.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
    finally:
        out.close()
        os.close(fd)
    idx = IDX_HDR.pack(IDX_MAGIC, CHUNK_SIZE, total, nchunks) + bytes(entries)
    with open(index_path, "wb") as f:
        f.write(idx)
        f.flush()
        os.fsync(f.fileno())
    return {"stream_bytes": used_total, "file_bytes": pos, "file_sha256": sha.hexdigest(),
            "index_bytes": len(idx), "index_sha256": hashlib.sha256(idx).hexdigest(),
            "chunks": stats, "bad_bytes": bad, "bad_ranges": bad_ranges, "chunk_size": CHUNK_SIZE}


def _pipeline_chunks(data_path: Path, idx: ChunkIndex, cancel: threading.Event):
    """Yield (i, offset, plain_size, flag, hash, plain_bytes or None, stored_bytes or None) in
    order, decompressing data chunks in parallel. Raises ChunkError for damaged chunks."""
    workers = max(1, min(os.cpu_count() or 1, 8))
    fd = os.open(data_path, os.O_RDONLY | os.O_CLOEXEC)

    def dec(i, raw, usz, flag, h):
        try:
            data = ZSTD.decompress(raw, usz) if flag == F_ZSTD else raw
        except Exception as e:  # noqa: BLE001
            raise ChunkError(errno.EIO, f"chunk {i}: {e}")
        if len(data) != usz or chunk_hash(data) != h:
            raise ChunkError(errno.EIO, f"chunk {i}: checksum mismatch")
        return data

    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pend: deque = deque()
            for i in range(idx.count):
                if cancel.is_set():
                    raise InterruptedError("cancelled")
                off, csz, usz, flag, h = idx.entry(i)
                if flag in (F_ZSTD, F_PLAIN):
                    raw = os.pread(fd, csz, off)
                    if len(raw) != csz:
                        raise ChunkError(errno.EIO, f"chunk {i}: backup file truncated")
                    fut = pool.submit(dec, i, raw, usz, flag, h)
                    pend.append((i, off, usz, flag, h, fut, raw))
                else:
                    pend.append((i, off, usz, flag, h, None, None))
                while len(pend) > 2 * workers:
                    j, o, u, fl, hh, f, rw = pend.popleft()
                    yield j, o, u, fl, hh, (f.result() if f else None), rw
            while pend:
                j, o, u, fl, hh, f, rw = pend.popleft()
                yield j, o, u, fl, hh, (f.result() if f else None), rw
    finally:
        os.close(fd)


def chunked_verify_files(data_path: Path, index_path: Path, it: dict, cancel: threading.Event,
                         progress=None) -> list[str]:
    """Check index + data file checksums and decode every chunk. Returns a list of problems."""
    problems: list[str] = []
    try:
        ib = Path(index_path).read_bytes()
    except OSError as e:
        return [f"index unreadable: {e.strerror}"]
    if it.get("index_sha256") and hashlib.sha256(ib).hexdigest() != it["index_sha256"]:
        problems.append("index checksum mismatch")
        return problems
    try:
        idx = ChunkIndex(index_path)
    except OSError as e:
        return [str(e)]
    sha = hashlib.sha256()
    expect = 0
    total = max(int(it.get("file_bytes") or 1), 1)
    done = 0
    gen = _pipeline_chunks(Path(data_path), idx, cancel)
    try:
        for i, off, usz, flag, h, data, raw in gen:
            if flag in (F_ZSTD, F_PLAIN):
                if off != expect:
                    problems.append(f"chunk {i}: unexpected position in data file")
                    break
                sha.update(raw)
                expect += len(raw)
                done += len(raw)
            if progress and i % 16 == 0:
                progress(done, total)
    except ChunkError as e:
        problems.append(str(e))
    except OSError as e:
        if isinstance(e, InterruptedError):
            raise
        problems.append(f"read error: {e}")
    finally:
        gen.close()
    if not problems:
        if expect != os.path.getsize(data_path):
            problems.append("data file has unexpected length")
        elif it.get("file_sha256") and sha.hexdigest() != it["file_sha256"]:
            problems.append("data file checksum mismatch")
    if progress:
        progress(total, total)
    return problems


def chunked_restore(data_path: Path, index_path: Path, node: str, base: int, limit: int,
                    cancel: threading.Event, progress=None) -> None:
    """Write all used chunks to node at base offset. Absent chunks are skipped."""
    idx = ChunkIndex(index_path)
    if idx.size > limit:
        raise ChunkError(errno.ENOSPC, f"image ({idx.size} B) larger than target area ({limit} B)")
    fd = os.open(node, os.O_WRONLY | os.O_CLOEXEC)
    done = flushed = 0
    try:
        for i, off, usz, flag, h, data, _raw in _pipeline_chunks(Path(data_path), idx, cancel):
            if flag == F_ABSENT:
                continue
            buf = data if data is not None else ZERO_CHUNK[:usz]
            pwrite_all(fd, buf, base + i * idx.chunk)
            done += usz
            if done - flushed >= 256 * MIB:
                # keep the amount of unwritten data small (honest progress, prompt Stop, and
                # write errors show up here instead of being lost in the page cache)
                os.fdatasync(fd)
                flushed = done
            if progress and i % 8 == 0:
                progress(i + 1, idx.count)
        os.fsync(fd)
    finally:
        os.close(fd)
    if progress:
        progress(idx.count, idx.count)


def chunked_verify_target(index_path: Path, node: str, base: int, cancel: threading.Event,
                          progress=None) -> list[int]:
    """Read back every written chunk from node and compare hashes. Returns bad chunk numbers."""
    idx = ChunkIndex(index_path)
    fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC)
    bad: list[int] = []
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        for i in range(idx.count):
            if cancel.is_set():
                raise InterruptedError("cancelled")
            _off, _csz, usz, flag, h = idx.entry(i)
            if flag == F_ABSENT:
                continue
            got = os.pread(fd, usz, base + i * idx.chunk)
            if len(got) != usz or chunk_hash(got) != h:
                bad.append(i)
            if progress and i % 8 == 0:
                progress(i + 1, idx.count)
    finally:
        os.close(fd)
    if progress:
        progress(idx.count, idx.count)
    return bad


def chunked_extract(data_path: Path, index_path: Path, out_path: Path) -> None:
    """Rebuild a sparse raw image (command line: diskimg --extract DATA INDEX OUT)."""
    idx = ChunkIndex(index_path)
    with open(out_path, "wb") as f:
        f.truncate(idx.size)
    chunked_restore(data_path, index_path, str(out_path), 0, idx.size, threading.Event())


def serve_chunks(mountpoint: str, specs: list[str]) -> int:
    """FUSE server (separate process): presents NAME.img files backed by chunked images."""
    try:
        import pyfuse3  # type: ignore
        import trio  # type: ignore
    except ImportError as e:
        print(f"diskimg: FUSE support missing ({e}). Install python-pyfuse3.", file=sys.stderr)
        return 2
    files: dict[int, tuple[bytes, ChunkReader]] = {}
    # specs: one JSON argument [[name, data_path, index_path], ...] (paths may contain any character)
    try:
        entries = json.loads(specs[0]) if len(specs) == 1 else None
        if not isinstance(entries, list):
            raise ValueError
    except ValueError:
        print("diskimg: bad --serve-chunks arguments", file=sys.stderr)
        return 2
    for n, (name, data, index) in enumerate(entries):
        files[2 + n] = ((str(name) + ".img").encode(), ChunkReader(data, index))
    names = {v[0]: k for k, v in files.items()}

    class Ops(pyfuse3.Operations):
        async def getattr(self, inode, ctx=None):
            e = pyfuse3.EntryAttributes()
            e.st_ino = inode
            e.attr_timeout = e.entry_timeout = 3600
            if inode == pyfuse3.ROOT_INODE:
                e.st_mode = stat.S_IFDIR | 0o755
            elif inode in files:
                e.st_mode = stat.S_IFREG | 0o444
                e.st_size = files[inode][1].size
                e.st_blksize = 65536
                e.st_blocks = (e.st_size + 511) // 512
            else:
                raise pyfuse3.FUSEError(errno.ENOENT)
            return e

        async def lookup(self, parent, name, ctx=None):
            if parent == pyfuse3.ROOT_INODE and name in names:
                return await self.getattr(names[name])
            raise pyfuse3.FUSEError(errno.ENOENT)

        async def opendir(self, inode, ctx):
            if inode != pyfuse3.ROOT_INODE:
                raise pyfuse3.FUSEError(errno.ENOTDIR)
            return inode

        async def readdir(self, fh, start_id, token):
            for ino in sorted(files):
                if ino <= start_id:
                    continue
                if not pyfuse3.readdir_reply(token, files[ino][0], await self.getattr(ino), ino):
                    break

        async def open(self, inode, flags, ctx):
            if inode not in files:
                raise pyfuse3.FUSEError(errno.ENOENT)
            if flags & (os.O_WRONLY | os.O_RDWR):
                raise pyfuse3.FUSEError(errno.EROFS)
            return pyfuse3.FileInfo(fh=inode, keep_cache=True)

        async def read(self, fh, off, size):
            reader = files[fh][1]
            try:
                return await trio.to_thread.run_sync(reader.read, off, size)
            except Exception as e:  # noqa: BLE001 - any failure is an I/O error, never a crash
                print(f"diskimg-fuse: {files[fh][0].decode()}: read at {off}: {e}", file=sys.stderr,
                      flush=True)
                raise pyfuse3.FUSEError(errno.EIO)

    opts = set(pyfuse3.default_options)
    opts |= {"fsname=diskimg", "ro"}
    pyfuse3.init(Ops(), mountpoint, opts)
    print("READY", flush=True)
    try:
        trio.run(pyfuse3.main)
    finally:
        pyfuse3.close(unmount=False)
    return 0


def serve_view(src: str, mountpoint: str, uid: int, gid: int, shared: bool = True) -> int:
    """FUSE server (separate process, root): a read-only view of the directory tree src in which
    every file and folder belongs to uid:gid and is readable by it (and only by it). Lets the
    desktop user copy anything out of an opened backup of a Linux filesystem, whose files keep
    their original owners and permissions. Never follows symlinks and opens only regular files."""
    try:
        import pyfuse3  # type: ignore
        import trio  # type: ignore
    except ImportError as e:
        print(f"diskimg: FUSE support missing ({e}). Install python-pyfuse3.", file=sys.stderr)
        return 2
    root = os.fsencode(src)
    paths: dict[int, bytes] = {pyfuse3.ROOT_INODE: root}
    keys: dict[int, tuple[int, int]] = {}
    by_key: dict[tuple[int, int], int] = {}
    nlookup: dict[int, int] = {}
    dirs: dict[int, list[tuple[bytes, os.stat_result]]] = {}
    files: dict[int, int] = {}
    counter = [pyfuse3.ROOT_INODE + 1]

    def err(e: OSError):
        return pyfuse3.FUSEError(e.errno or errno.EIO)

    def attrs(ino: int, st: os.stat_result):
        a = pyfuse3.EntryAttributes()
        a.st_ino = ino
        a.generation = 0
        a.entry_timeout = a.attr_timeout = 300
        m = st.st_mode
        if stat.S_ISDIR(m):
            mode = 0o500
        elif stat.S_ISLNK(m):
            mode = 0o777
        elif stat.S_ISREG(m):
            mode = 0o400 | (0o100 if m & 0o111 else 0)
        else:       # devices, FIFOs, sockets: listed, never opened (the kernel would handle a FIFO
            mode = 0    # itself and block; no permission bits make every open fail at once)
        a.st_mode = stat.S_IFMT(m) | mode
        a.st_nlink = st.st_nlink
        a.st_uid, a.st_gid = uid, gid
        a.st_rdev = 0
        a.st_size = st.st_size
        a.st_blksize = 131072
        a.st_blocks = st.st_blocks
        a.st_atime_ns, a.st_mtime_ns, a.st_ctime_ns = st.st_atime_ns, st.st_mtime_ns, st.st_ctime_ns
        return a

    def ino_for(path: bytes, st: os.stat_result) -> int:
        k = (st.st_dev, st.st_ino)
        ino = by_key.get(k)
        if ino is None:
            ino = counter[0]
            counter[0] += 1
            by_key[k] = ino
            keys[ino] = k
            paths[ino] = path
            nlookup[ino] = 0
        return ino

    def drop(ino: int) -> None:
        if ino != pyfuse3.ROOT_INODE and nlookup.get(ino, 1) <= 0:
            nlookup.pop(ino, None)
            paths.pop(ino, None)
            by_key.pop(keys.pop(ino, None), None)

    def path_of(ino: int) -> bytes:
        try:
            return paths[ino]
        except KeyError:
            raise pyfuse3.FUSEError(errno.ESTALE) from None

    def list_dir(path: bytes) -> list[tuple[bytes, os.stat_result]]:
        out = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    out.append((de.name, de.stat(follow_symlinks=False)))
                except OSError:
                    continue
        out.sort()
        return out

    class Ops(pyfuse3.Operations):
        async def getattr(self, inode, ctx=None):
            try:
                st = os.lstat(path_of(inode))
            except OSError as e:
                raise err(e)
            return attrs(inode, st)

        async def lookup(self, parent, name, ctx=None):
            if name in (b".", b".."):
                raise pyfuse3.FUSEError(errno.ENOENT)
            p = os.path.join(path_of(parent), name)
            try:
                st = os.lstat(p)
            except OSError as e:
                raise err(e)
            ino = ino_for(p, st)
            nlookup[ino] += 1
            return attrs(ino, st)

        async def forget(self, inode_list):
            for ino, n in inode_list:
                if ino in nlookup:
                    nlookup[ino] -= n
                    drop(ino)

        async def readlink(self, inode, ctx):
            try:
                return os.readlink(path_of(inode))
            except OSError as e:
                raise err(e)

        async def opendir(self, inode, ctx):
            p = path_of(inode)
            try:
                entries = await trio.to_thread.run_sync(list_dir, p)
            except OSError as e:
                raise err(e)
            fh = counter[0]
            counter[0] += 1
            dirs[fh] = [(os.path.join(p, n), st) for n, st in entries]
            return fh

        async def readdir(self, fh, start_id, token):
            entries = dirs.get(fh, [])
            for i in range(start_id, len(entries)):
                p, st = entries[i]
                ino = ino_for(p, st)
                if not pyfuse3.readdir_reply(token, os.path.basename(p), attrs(ino, st), i + 1):
                    drop(ino)
                    break
                nlookup[ino] += 1

        async def releasedir(self, fh):
            dirs.pop(fh, None)

        async def open(self, inode, flags, ctx):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
                raise pyfuse3.FUSEError(errno.EROFS)
            try:
                fd = os.open(path_of(inode), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
            except OSError as e:
                raise err(e)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise pyfuse3.FUSEError(errno.EACCES)
            files[fd] = inode
            return pyfuse3.FileInfo(fh=fd, keep_cache=True)

        async def read(self, fh, off, size):
            try:
                return await trio.to_thread.run_sync(os.pread, fh, size, off)
            except OSError as e:
                raise err(e)

        async def release(self, fh):
            if files.pop(fh, None) is not None:
                os.close(fh)

        async def statfs(self, ctx):
            s = pyfuse3.StatvfsData()
            try:
                v = os.statvfs(root)
            except OSError as e:
                raise err(e)
            for f in ("f_bsize", "f_frsize", "f_blocks", "f_bfree", "f_bavail", "f_files", "f_ffree",
                      "f_favail", "f_namemax"):
                setattr(s, f, getattr(v, f))
            return s

    opts = set(pyfuse3.default_options)
    opts |= {"fsname=diskimg-view", "subtype=diskimg", "ro", "default_permissions", "nodev", "nosuid"}
    if shared:                      # root serves it to the desktop user (tests run unprivileged)
        opts.add("allow_other")
    pyfuse3.init(Ops(), mountpoint, opts)
    print("READY", flush=True)
    try:
        trio.run(pyfuse3.main)
    finally:
        pyfuse3.close(unmount=False)
    return 0


def start_view(src: Path, mp: Path) -> subprocess.Popen:
    """Show src (a root-only mount) at mp, with every file readable by the desktop user (or root).
    Returns the server process; raises ValueError when it cannot start."""
    du = desktop_user()
    uid, gid = (du[1], du[2]) if du else (0, 0)
    mp.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logpath = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_view.log"
    with open(logpath, "a") as log:
        p = subprocess.Popen([sys.executable, "-I", str(RUN_SCRIPT), "serve-view", str(src), str(mp),
                              str(uid), str(gid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                             start_new_session=True)
    _reg(p)
    import select
    r, _, _ = select.select([p.stdout], [], [], 30)
    line = p.stdout.readline().decode(errors="replace").strip() if r else ""
    if line == "READY":
        return p
    if p.poll() is None:
        p.kill()
    p.wait()
    _unreg(p)
    try:
        tail = logpath.read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        tail = "no output"
    raise ValueError(f"the file view did not start: {tail}")


def view_available() -> bool:
    """Can start_view() work here (python-pyfuse3 + python-trio + FUSE)?"""
    import importlib.util
    return (os.path.exists("/dev/fuse") and importlib.util.find_spec("pyfuse3") is not None
            and importlib.util.find_spec("trio") is not None)


def stage_dir(tag: str) -> Path:
    """A fresh root-only directory for a mount the desktop user sees only through start_view()."""
    rd = Path("/run/diskimg")
    rd.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(rd, 0o700)
    d = Path(tempfile.mkdtemp(dir=rd, prefix=f"stage-{sanitize(tag)[:40]}-"))
    return d


_usage_lock = threading.Lock()
_usage_procs: set[subprocess.Popen] = set()
_usage_gen = 0          # bumped by cancel_usage(); scans of an older generation must not start
_usage_block = 0        # > 0 while a job screen is open: no background scans at all


def usage_generation() -> int:
    with _usage_lock:
        return _usage_gen


def usage_allowed(gen: int) -> bool:
    with _usage_lock:
        return _usage_block == 0 and gen == _usage_gen


def cancel_usage() -> None:
    """Stop every running background usage scan and invalidate scans that are about to start
    (before a job touches the disks)."""
    global _usage_gen
    with _usage_lock:
        _usage_gen += 1
        procs = list(_usage_procs)
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def block_usage(on: bool) -> None:
    global _usage_block
    with _usage_lock:
        _usage_block = max(0, _usage_block + (1 if on else -1))
    if on:
        cancel_usage()


def domain_regions(tool: str, node: str, log: Path, background: bool = False, gen: int | None = None,
                   cancel=None, timeout: float = 1800) -> list[tuple[int, int]]:
    """Used regions of a filesystem according to partclone (-D reads only the allocation map).
    background=True: a usage scan of generation gen (refused/killed by cancel_usage()).
    cancel: event-like object; the scan is killed when it is set."""
    rd = Path("/run/diskimg")
    rd.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=rd, prefix="domain-")
    os.close(fd)
    ofd, outname = tempfile.mkstemp(dir=rd, prefix="domain-out-")
    p = None
    try:
        cmd = [tool, "-D", "-s", node, "-O", tmp, "-L", str(log)]
        with _usage_lock:
            if background and (_usage_block or gen != _usage_gen):
                raise ValueError("usage scan cancelled")
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=ofd, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            if background:
                _usage_procs.add(p)
            else:
                _reg(p)
        t0 = time.monotonic()
        while True:
            try:
                rc = p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if (cancel is not None and cancel.is_set()) or time.monotonic() - t0 > timeout:
                p.kill()
                p.wait()
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("cancelled")
                raise ValueError(f"{tool} took too long to read the allocation map")
        if rc != 0:
            with open(outname, errors="replace") as f:
                out = f.read()
            tail = [x for x in out.replace("\r", "\n").splitlines() if x.strip()]
            raise ValueError(f"{tool} could not read the allocation map: " + " | ".join(tail[-2:]))
        return [(o, n) for o, n, st in parse_mapfile(Path(tmp)) if st == "+"]
    finally:
        if p is not None:
            with _usage_lock:
                _usage_procs.discard(p)
            _unreg(p)
        os.close(ofd)
        for f in (tmp, outname):
            try:
                os.unlink(f)
            except OSError:
                pass


def fs_usage(dev: "Dev", gen: int, scan: bool) -> tuple[int, int] | None:
    """(used bytes, size) of the filesystem on dev, or None. Read-only.
    Unmounted filesystems are only scanned (partclone reads the allocation map) when scan=True."""
    if dev.mounts:
        try:
            st = os.statvfs(dev.mounts[0])
            return (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_blocks * st.f_frsize
        except OSError:
            return None
    if not scan:
        return None
    fstype = dev.fstype or ("" if dev.busy else blkid_probe(dev.path).get("TYPE", ""))
    tool = pc_tool(fstype, True) if fstype else None
    if not tool or dev.busy:
        return None
    try:
        regs = domain_regions(tool, dev.path, LOG_DIR / "usage-partclone.log", background=True,
                              gen=gen, timeout=600)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None
    return sum(n for _, n in regs), dev.size


# ----------------------------------------------------------------------------
#  File backups (restic)
# ----------------------------------------------------------------------------
FILESETS = CONF_DIR / "filesets.json"
RESTIC_PW_DIR = CONF_DIR / "restic"
RESTIC_CACHE = Path("/var/cache/diskimg/restic")
SRC_ROOT = Path("/mnt/diskimg-src")
FILES_MNT = Path("/mnt/diskimg-files")
KEEP_POLICIES = {
    "": ("Keep all versions", []),
    "std": ("Keep 7 daily, 8 weekly, 24 monthly",
            ["--keep-daily", "7", "--keep-weekly", "8", "--keep-monthly", "24"]),
    "long": ("Keep 14 daily, 12 weekly, 24 monthly, 10 yearly",
             ["--keep-daily", "14", "--keep-weekly", "12", "--keep-monthly", "24", "--keep-yearly", "10"]),
    "last30": ("Keep the last 30 versions", ["--keep-last", "30"]),
}
DEFAULT_EXCLUDES = ["pagefile.sys", "hiberfil.sys", "swapfile.sys", "$Recycle.Bin",
                    "System Volume Information", "*.tmp", ".cache"]


def load_filesets() -> list[dict]:
    try:
        data = json.loads(FILESETS.read_text())
        return [d for d in data if isinstance(d, dict) and d.get("id")]
    except (OSError, ValueError):
        return []


def save_filesets(items: list[dict]) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    tmp = FILESETS.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, FILESETS)


def restic_pwfile(fs: dict) -> Path:
    return RESTIC_PW_DIR / f"{fs['id']}.pw"


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")


def restic_base(fs: dict) -> list[str]:
    RESTIC_CACHE.mkdir(parents=True, exist_ok=True)
    return [shutil.which("restic") or "restic", "-r", fs["repo"], "--password-file",
            str(restic_pwfile(fs)), "--cache-dir", str(RESTIC_CACHE)]


def repo_location_problem(repo: str) -> str | None:
    """Why a NEW restic repository must not be created at repo, or None.
    Prevents the classic failure: the NAS / USB disk is not connected, the folder is created on
    the local system disk instead, and every 'successful' backup lands in the wrong place."""
    p = Path(repo)
    if not p.is_absolute():
        return "The repository folder must be an absolute path."
    anc = existing_ancestor(p)
    fst = fstype_of(anc)
    ram = fst in ("tmpfs", "ramfs", "overlay", "squashfs")
    if any(p == r or r in p.parents for r in (Path("/mnt"), Path("/media"), Path("/run/media"))):
        try:
            on_root = os.stat(anc).st_dev == os.stat("/").st_dev
        except OSError:
            on_root = True
        if on_root or ram:
            return (f"{repo} is not on a connected disk or network share (only {anc} exists, and it "
                    "is not a mounted disk). Connect the NAS / disk first.")
    if ram:
        return f"{anc} is in RAM ({fst}); the backup would be lost at shutdown. Choose a real disk."
    return None


def repo_exists(repo: str) -> bool:
    return bool(repo) and (Path(repo) / "config").is_file()


def restic_password_ok(repo: str, pw: str) -> bool:
    env = dict(os.environ, RESTIC_PASSWORD=pw)
    r = subprocess.run([shutil.which("restic") or "restic", "-r", repo, "--no-cache", "cat", "config"],
                       capture_output=True, text=True, env=env, timeout=120)
    return r.returncode == 0


def restic_snapshots(fs: dict) -> list[dict] | None:
    if not repo_exists(fs.get("repo", "")) or not restic_pwfile(fs).exists():
        return None
    try:
        r = run(restic_base(fs) + ["snapshots", "--json", "--tag", f"diskimg-set:{fs['id']}"], timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (ValueError, subprocess.TimeoutExpired):
        return None


def find_by_uuid(uuid: str) -> str | None:
    if not uuid:
        return None
    r = run(["blkid", "-U", uuid], timeout=20)
    node = r.stdout.strip()
    return node if r.returncode == 0 and node else None


# ----------------------------------------------------------------------------
#  Streaming engine
# ----------------------------------------------------------------------------
class Hasher:
    """sha256 of a stream. For partclone streams also a 'payload' hash that
    ignores the header fields that legitimately differ between source and
    target (partclone version, device size, header CRC)."""
    HDR = 110

    def __init__(self, partclone: bool = False):
        self.full = hashlib.sha256()
        self.payload = hashlib.sha256() if partclone else None
        self.n = 0
        self._head = bytearray()
        self._head_done = not partclone

    def update(self, b: bytes) -> None:
        self.full.update(b)
        if self.payload is not None:
            if not self._head_done:
                need = self.HDR - len(self._head)
                self._head += b[:need]
                b = b[need:]
                if len(self._head) == self.HDR:
                    self._flush_head()
            if self._head_done and b:
                self.payload.update(b)

    def feed(self, b: bytes) -> None:
        self.update(b)
        self.n += len(b)

    def _flush_head(self) -> None:
        h = bytes(self._head)
        if h[30:34] == b"0002":
            self.payload.update(h[36:52])
            self.payload.update(h[60:106])
        else:
            self.payload.update(h)
        self._head_done = True

    def hexdigest(self) -> str:
        return self.full.hexdigest()

    def payload_hex(self) -> str | None:
        if self.payload is None:
            return None
        if not self._head_done:
            self.payload.update(bytes(self._head))
            self._head_done = True
        return self.payload.hexdigest()


class PtyWatch:
    """Reads a child's terminal output (partclone) and extracts progress."""
    PCT = re.compile(r"Completed:\s*([\d.]+)%")
    RATE = re.compile(r"Rate:\s*([\d.]+\s*[A-Za-z]+/min)")
    BAD = re.compile(r"Can't read sector at (\d+)")
    ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def __init__(self):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        self.pct: float | None = None
        self.rate = ""
        self.bad_sectors = 0
        self.bad_offsets: list[int] = []
        self.tail: deque[str] = deque(maxlen=12)
        self.th: threading.Thread | None = None

    def start(self) -> None:
        os.close(self.slave)
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def _loop(self) -> None:
        buf = ""
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            buf += data.decode(errors="replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                self._line(self.ANSI.sub("", p).strip())
        if buf.strip():
            self._line(self.ANSI.sub("", buf).strip())
        try:
            os.close(self.master)
        except OSError:
            pass

    def _line(self, s: str) -> None:
        if not s:
            return
        m = self.PCT.search(s)
        if m:
            try:
                self.pct = float(m.group(1))
            except ValueError:
                pass
            r = self.RATE.search(s)
            if r:
                self.rate = r.group(1)
            return
        if s.startswith("Current block"):
            return
        m = self.BAD.search(s)
        if m:
            self.bad_sectors += 1
            if len(self.bad_offsets) < 1000:
                self.bad_offsets.append(int(m.group(1)))
            return
        if set(s) <= set("*= "):
            return
        self.tail.append(s)

    def join(self) -> None:
        if self.th:
            self.th.join(timeout=5)


class Src:
    total = 0
    name = "source"

    def read(self, n: int) -> bytes:
        raise NotImplementedError

    def fraction(self) -> float:
        return 0.0

    def bad_bytes(self) -> int:
        return 0

    def bad_ranges(self) -> list[list[int]]:
        return []

    def finish(self) -> str | None:
        return None

    def kill(self) -> None:
        pass


class FileSrc(Src):
    def __init__(self, path: Path | str):
        self.name = str(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self.total = os.fstat(self.fd).st_size
        self.pos = 0
        try:
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass

    def read(self, n: int) -> bytes:
        b = os.read(self.fd, n)
        self.pos += len(b)
        if self.pos % (256 * MIB) < len(b):
            try:
                os.posix_fadvise(self.fd, 0, self.pos, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        return b

    def fraction(self) -> float:
        return self.pos / self.total if self.total else 1.0

    def finish(self) -> str | None:
        os.close(self.fd)
        return None


class BytesSrc(Src):
    def __init__(self, data: bytes):
        self.data, self.pos, self.total = data, 0, len(data)

    def read(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]
        self.pos += len(b)
        return b

    def fraction(self) -> float:
        return self.pos / self.total if self.total else 1.0


class RawSrc(Src):
    """Reads a device range. With rescue=True unreadable 4 KiB blocks are
    replaced by zeros and recorded instead of aborting."""
    GRAIN = 4096

    def __init__(self, path: str, offset: int, length: int, rescue: bool = True):
        self.name = path
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self.start, self.pos, self.end = offset, offset, offset + length
        self.total = length
        self.rescue = rescue
        self.bad = 0
        self.ranges: list[list[int]] = []
        try:
            os.posix_fadvise(self.fd, offset, length, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass

    def _pread_exact(self, n: int, off: int) -> bytes:
        b = os.pread(self.fd, n, off)
        if len(b) != n:
            raise OSError(errno.EIO, f"short read at {off} ({len(b)} of {n} bytes)")
        return b

    def _mark_bad(self, off: int, n: int) -> None:
        self.bad += n
        if self.ranges and self.ranges[-1][0] + self.ranges[-1][1] == off:
            self.ranges[-1][1] += n
        elif len(self.ranges) < 1000:
            self.ranges.append([off, n])

    def _rescue(self, off: int, n: int) -> bytes:
        out = bytearray()
        end = off + n
        p = off
        while p < end:
            q = min(end, (p // (64 * 1024) + 1) * 64 * 1024)
            try:
                out += self._pread_exact(q - p, p)
            except OSError:
                s = p
                while s < q:
                    t = min(q, (s // self.GRAIN + 1) * self.GRAIN)
                    try:
                        out += self._pread_exact(t - s, s)
                    except OSError:
                        out += b"\0" * (t - s)
                        self._mark_bad(s, t - s)
                    s = t
            p = q
        return bytes(out)

    def read(self, n: int) -> bytes:
        if self.pos >= self.end:
            return b""
        n = min(n, self.end - self.pos)
        try:
            b = self._pread_exact(n, self.pos)
        except OSError:
            if not self.rescue:
                raise
            b = self._rescue(self.pos, n)
        self.pos += len(b)
        if (self.pos - self.start) % (256 * MIB) < len(b):
            try:
                os.posix_fadvise(self.fd, self.start, self.pos - self.start, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        return b

    def fraction(self) -> float:
        return (self.pos - self.start) / self.total if self.total else 1.0

    def bad_bytes(self) -> int:
        return self.bad

    def bad_ranges(self) -> list[list[int]]:
        return [[o - self.start, n] for o, n in self.ranges]

    def finish(self) -> str | None:
        os.close(self.fd)
        return None


class CmdSrc(Src):
    """A process writing the stream to stdout (partclone -c ... -o -)."""

    def __init__(self, cmd: list[str], sector: int = 512):
        self.name = cmd[0]
        self.cmd = cmd
        self.w = PtyWatch()
        self.sector = sector
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=self.w.slave, close_fds=True, start_new_session=True)
        _reg(self.p)
        self.w.start()
        self.fd = self.p.stdout.fileno()

    def read(self, n: int) -> bytes:
        return os.read(self.fd, n)

    def fraction(self) -> float:
        return (self.w.pct or 0.0) / 100

    def bad_bytes(self) -> int:
        return self.w.bad_sectors * 512

    def bad_ranges(self) -> list[list[int]]:
        return [[o, 512] for o in self.w.bad_offsets]

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()

    def finish(self) -> str | None:
        rc = self.p.wait()
        self.p.stdout.close()
        self.w.join()
        _unreg(self.p)
        if rc != 0:
            return f"{self.name} exited with code {rc}: " + " | ".join(list(self.w.tail)[-4:])
        return None


class Dst:
    name = "target"

    def write(self, b: bytes) -> None:
        raise NotImplementedError

    def finish(self) -> str | None:
        return None

    def kill(self) -> None:
        pass

    def fraction(self) -> float | None:
        return None


class NullDst(Dst):
    def write(self, b: bytes) -> None:
        pass


class BytesDst(Dst):
    def __init__(self):
        self.buf = bytearray()

    def write(self, b: bytes) -> None:
        self.buf += b


class FileDst(Dst):
    def __init__(self, path: Path):
        self.name = str(path)
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        self.n = 0

    def write(self, b: bytes) -> None:
        write_all(self.fd, b)
        self.n += len(b)
        if self.n % (256 * MIB) < len(b):
            try:
                os.fdatasync(self.fd)
                os.posix_fadvise(self.fd, 0, self.n, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass

    def finish(self) -> str | None:
        try:
            os.fsync(self.fd)
            try:
                os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        except OSError as e:
            return f"fsync {self.name}: {e.strerror}"
        finally:
            os.close(self.fd)
        return None


class SparseFileDst(Dst):
    """Writes a regular file and leaves holes for all-zero 64 KiB blocks."""
    Z = bytes(65536)

    def __init__(self, path: Path, size: int):
        self.name = str(path)
        # O_EXCL | O_NOFOLLOW: never follow a symlink someone placed at this name
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.pos = 0
        self.size = size

    def write(self, b: bytes) -> None:
        mv = memoryview(b)
        n = len(b)
        i = 0
        while i < n:
            k = min(n, i + 65536)
            chunk = mv[i:k]
            if chunk != self.Z[: k - i]:
                pwrite_all(self.fd, chunk, self.pos + i)
            i = k
        self.pos += n

    def finish(self) -> str | None:
        try:
            os.ftruncate(self.fd, max(self.size, self.pos))
            os.fsync(self.fd)
        except OSError as e:
            return f"{self.name}: {e.strerror}"
        finally:
            os.close(self.fd)
        return None


class RawDst(Dst):
    def __init__(self, path: str, offset: int, limit: int):
        self.name = path
        self.fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        self.start, self.pos, self.end = offset, offset, offset + limit
        self.flushed = offset

    def write(self, b: bytes) -> None:
        if self.pos + len(b) > self.end:
            raise OSError(errno.ENOSPC, f"data does not fit into {self.name}")
        pwrite_all(self.fd, b, self.pos)
        self.pos += len(b)
        if self.pos - self.flushed >= 256 * MIB:
            # write back regularly: honest progress, prompt Stop, write errors reported here
            os.fdatasync(self.fd)
            try:
                os.posix_fadvise(self.fd, self.flushed, self.pos - self.flushed, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
            self.flushed = self.pos

    def finish(self) -> str | None:
        try:
            os.fsync(self.fd)
        except OSError as e:
            return f"fsync {self.name}: {e.strerror}"
        finally:
            os.close(self.fd)
        return None


class CmdDst(Dst):
    """A process reading the stream on stdin (partclone -r -s - -o DEV)."""

    def __init__(self, cmd: list[str]):
        self.name = cmd[0]
        self.w = PtyWatch()
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=self.w.slave,
                                  stderr=self.w.slave, close_fds=True, start_new_session=True)
        _reg(self.p)
        self.w.start()
        self.fd = self.p.stdin.fileno()

    def write(self, b: bytes) -> None:
        try:
            write_all(self.fd, b)
        except BrokenPipeError:
            raise OSError(errno.EPIPE, f"{self.name} stopped reading")

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()

    def fraction(self) -> float | None:
        return None if self.w.pct is None else self.w.pct / 100

    def finish(self) -> str | None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait()
        self.w.join()
        _unreg(self.p)
        if rc != 0:
            return f"{self.name} exited with code {rc}: " + " | ".join(list(self.w.tail)[-4:])
        return None


class Crypto:
    """gpg symmetric encryption with a private, temporary GNUPGHOME."""

    def __init__(self, passphrase: str):
        self.passphrase = passphrase
        self.home = tempfile.mkdtemp(prefix="diskimg-gpg-")
        os.chmod(self.home, 0o700)

    def base(self) -> list[str]:
        return ["gpg", "--homedir", self.home, "--batch", "--yes", "--quiet", "--no-tty",
                "--no-symkey-cache", "--pinentry-mode", "loopback", "--passphrase-fd", "{FD}"]

    def enc(self) -> tuple[list[str], str]:
        return (self.base() + ["--symmetric", "--cipher-algo", "AES256", "--compress-algo", "none",
                               "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
                               "--output", "-"], self.passphrase)

    def dec(self) -> tuple[list[str], str]:
        return (self.base() + ["--decrypt", "--output", "-"], self.passphrase)

    def close(self) -> None:
        if shutil.which("gpgconf"):
            subprocess.run(["gpgconf", "--homedir", self.home, "--kill", "gpg-agent"],
                           capture_output=True, timeout=20)
        shutil.rmtree(self.home, ignore_errors=True)


def encode_filters(level: int, crypto: Crypto | None) -> list[tuple[list[str], str | None]]:
    f: list[tuple[list[str], str | None]] = []
    if level:
        f.append((["zstd", "-q", "-T0", f"-{level}", "-c"], None))
    if crypto:
        f.append(crypto.enc())
    return f


def decode_filters(man: dict, crypto: Crypto | None, item: dict | None = None
                   ) -> list[tuple[list[str], str | None]]:
    f: list[tuple[list[str], str | None]] = []
    if item is not None and item.get("codec") == "plain":
        return f
    if man.get("encryption"):
        if not crypto:
            raise ValueError("passphrase required")
        f.append(crypto.dec())
    if man.get("compression"):
        f.append((["zstd", "-q", "-d", "-c"], None))
    return f


def test_passphrase(cdir: Path, man: dict, passphrase: str) -> bool:
    item = next((i for i in man.get("items", []) if i.get("file")), None)
    if not item:
        return True
    c = Crypto(passphrase)
    try:
        cmd, secret = c.dec()
        r, w = os.pipe()
        os.write(w, secret.encode() + b"\n")
        os.close(w)
        cmd = [x.replace("{FD}", str(r)) for x in cmd]
        with open(cdir / item["file"], "rb") as fin:
            p = subprocess.Popen(cmd, stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 pass_fds=(r,))
        os.close(r)
        got = p.stdout.read(1)
        p.kill()
        p.wait()
        return len(got) == 1
    finally:
        c.close()


class Transfer:
    """src -> [hash A] -> filters... -> [hash B] -> dst"""

    def __init__(self, src: Src, dst: Dst, filters=(), hA: Hasher | None = None,
                 hB: Hasher | None = None):
        self.src, self.dst, self.filters = src, dst, list(filters)
        self.hA = hA or Hasher()
        self.hB = hB if hB is not None else (self.hA if not self.filters else Hasher())
        self.cancel = threading.Event()
        self.procs: list[subprocess.Popen] = []
        self.errfiles = []
        self.bytes_in = 0
        self.bytes_out = 0
        self.src_error: str | None = None
        self.dst_error: str | None = None
        self.filter_errors: list[str] = []
        self.t0 = time.monotonic()
        self._th: threading.Thread | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    @property
    def errors(self) -> list[str]:
        e = []
        if self.src_error:
            e.append(f"read: {self.src_error}")
        e += self.filter_errors
        if self.dst_error:
            e.append(f"write: {self.dst_error}")
        return e

    def abort(self) -> None:
        self.cancel.set()
        self._kill()

    def _kill(self) -> None:
        try:
            self.src.kill()
        except Exception:
            pass
        try:
            self.dst.kill()
        except Exception:
            pass
        for p in self.procs:
            if p.poll() is None:
                p.kill()

    def fraction(self) -> float:
        f = self.src.fraction()
        d = self.dst.fraction()
        return f if (f > 0 or d is None) else d

    def _spawn(self) -> None:
        for i, (cmd, secret) in enumerate(self.filters):
            ef = tempfile.TemporaryFile()
            pass_fds: tuple[int, ...] = ()
            rfd = None
            if secret is not None:
                rfd, wfd = os.pipe()
                os.write(wfd, secret.encode() + b"\n")
                os.close(wfd)
                cmd = [c.replace("{FD}", str(rfd)) for c in cmd]
                pass_fds = (rfd,)
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE if i == 0 else self.procs[-1].stdout,
                                 stdout=subprocess.PIPE, stderr=ef, pass_fds=pass_fds,
                                 start_new_session=True)
            if rfd is not None:
                os.close(rfd)
            if i > 0:
                self.procs[-1].stdout.close()
            _reg(p)
            self.procs.append(p)
            self.errfiles.append(ef)

    def run(self, on_progress=None) -> bool:
        last = 0.0
        feed_err: list[str] = []

        def tick():
            nonlocal last
            now = time.monotonic()
            if on_progress and now - last > 0.3:
                last = now
                on_progress(self)

        try:
            if not self.filters:
                while True:
                    if self.cancel.is_set():
                        raise InterruptedError("cancelled")
                    try:
                        b = self.src.read(CHUNK)
                    except OSError as e:
                        self.src_error = f"{self.src.name}: {e.strerror or e}"
                        raise
                    if not b:
                        break
                    self.bytes_in += len(b)
                    self.hA.feed(b)
                    if self.hB is not self.hA:
                        self.hB.feed(b)
                    try:
                        self.dst.write(b)
                    except OSError as e:
                        self.dst_error = f"{self.dst.name}: {e.strerror or e}"
                        raise
                    self.bytes_out += len(b)
                    tick()
            else:
                self._spawn()
                first = self.procs[0]

                def feeder():
                    try:
                        w = first.stdin.fileno()
                        while True:
                            if self.cancel.is_set():
                                return
                            try:
                                b = self.src.read(CHUNK)
                            except OSError as e:
                                self.src_error = f"{self.src.name}: {e.strerror or e}"
                                return
                            if not b:
                                return
                            self.bytes_in += len(b)
                            self.hA.feed(b)
                            write_all(w, b)
                    except OSError as e:
                        feed_err.append(str(e))
                    finally:
                        try:
                            first.stdin.close()
                        except OSError:
                            pass

                th = threading.Thread(target=feeder, daemon=True)
                self._th = th
                th.start()
                rfd = self.procs[-1].stdout.fileno()
                while True:
                    if self.cancel.is_set():
                        raise InterruptedError("cancelled")
                    b = os.read(rfd, CHUNK)
                    if not b:
                        break
                    self.hB.feed(b)
                    try:
                        self.dst.write(b)
                    except OSError as e:
                        self.dst_error = f"{self.dst.name}: {e.strerror or e}"
                        raise
                    self.bytes_out += len(b)
                    tick()
                th.join()
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, InterruptedError) and not (self.src_error or self.dst_error):
                self.dst_error = str(e)
            self._kill()
        finally:
            if self._th is not None:
                self._th.join(timeout=120)
            for p in self.procs:
                try:
                    p.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                _unreg(p)
            if self.procs:
                try:
                    self.procs[-1].stdout.close()
                except OSError:
                    pass
            se = None
            try:
                se = self.src.finish()
            except Exception as e:  # noqa: BLE001
                se = str(e)
            if se and not self.src_error:
                self.src_error = se
            de = None
            try:
                de = self.dst.finish()
            except Exception as e:  # noqa: BLE001
                de = str(e)
            if de and not self.dst_error:
                self.dst_error = de
            for (cmd, _), p, ef in zip(self.filters, self.procs, self.errfiles):
                if p.returncode not in (0, None) and not self.cancel.is_set():
                    ef.seek(0)
                    msg = ef.read().decode(errors="replace").strip().replace("\n", " | ")
                    self.filter_errors.append(f"{cmd[0]} exited with code {p.returncode}: {msg[:300]}")
                ef.close()
            if feed_err and not self.cancel.is_set() and not self.filter_errors and not self.src_error:
                self.filter_errors.append("pipe error: " + feed_err[0])
        if on_progress:
            on_progress(self)
        return not self.errors and not self.cancel.is_set()


# ----------------------------------------------------------------------------
#  Job
# ----------------------------------------------------------------------------
@dataclass
class Job:
    mode: str
    engine: str                 # ddrescue | container | check
    method: str                 # raw | fs
    src: str
    dst: str
    size: int
    src_label: str
    dst_label: str
    src_dev: Dev | None
    dst_dev: Dev | None
    mapfile: Path | None = None
    verify: bool = True
    sparse: bool = True
    zero_fill: bool = True
    fix_gpt: bool = True
    idirect: bool = False
    retries: int = 1
    resume: bool = False
    replace_existing: bool = False
    compression: int = 0
    encrypt: bool = False
    passphrase: str | None = None
    allow_btrfs: bool = False
    fallback_raw: bool = True
    precheck: bool = True
    layout: Layout | None = None
    manifest: dict | None = None
    browse_ids: list[str] = field(default_factory=list)
    keep_cache: bool = False
    fileset: dict | None = None
    shrink: "ShrinkPlan | None" = None    # restore onto a smaller disk (see shrink_plan)

    @property
    def dst_is_dev(self) -> bool:
        return self.dst_dev is not None

    @property
    def dst_size(self) -> int:
        return self.dst_dev.size if self.dst_dev else self.size


def engine_label(job_or_mode, method: str = "raw", comp: int = 0, enc: bool = False,
                 src_container: bool = False) -> str:
    mode = job_or_mode
    if mode == "fbk":
        return "restic: incremental file backup"
    if mode == "brw":
        return "mount read-only (chunked: instant, older: unpack)"
    if mode == "chk":
        return "check: re-read and compare checksums"
    if mode == "i2d":
        return ("restore .dimg container" if src_container
                else "ddrescue (resumable) from raw .img")
    if method == "fs":
        if mode == "d2i":
            return ("stream: used blocks, zstd+gpg (browse needs unpacking)" if enc else
                    "chunked: used blocks only, instant browse -> .dimg folder")
        return "partclone: used blocks only"
    if mode == "d2d" or (not comp and not enc):
        return "ddrescue: every sector, resumable" + (" -> .img file" if mode == "d2i" else "")
    if not enc:
        return "chunked: every sector, zstd, instant browse -> .dimg folder"
    return "stream: every sector, " + "+".join(
        x for x in ("zstd" if comp else "", "gpg") if x) + " -> .dimg folder"


DDR_KEYS = [
    ("rescued", "Rescued"), ("pct rescued", "Progress"), ("current rate", "Current rate"),
    ("average rate", "Average rate"), ("remaining time", "Remaining"), ("run time", "Run time"),
    ("non-tried", "Not tried yet"), ("bad-sector", "Bad sectors"), ("bad areas", "Bad areas"),
    ("read errors", "Read errors"), ("non-trimmed", "Non-trimmed"), ("non-scraped", "Non-scraped"),
]


def job_steps(j: Job) -> list[tuple[str, str]]:
    if j.engine == "ddrescue":
        return [("copy", "Copy (ddrescue)"), ("fill", "Zero-fill unreadable areas"),
                ("sync", "Flush to disk"), ("verify", "Verify"), ("gpt", "Fix GPT backup header")]
    if j.engine == "check":
        return [("prep", "Read backup"), ("verify", "Check all data")]
    if j.engine == "browse":
        return [("prep", "Read backup"), ("unpack", "Prepare partitions"), ("mount", "Mount read-only")]
    if j.engine == "files":
        return [("prep", "Prepare sources"), ("repo", "Open repository"), ("backup", "Back up files")]
    if j.engine == "files-check":
        return [("repo", "Open repository"), ("check", "Check repository")]
    if j.mode == "d2i":
        return [("prep", "Analyse source"), ("copy", "Copy data"), ("meta", "Write manifest"),
                ("verify", "Verify image")]
    steps = [("prep", "Prepare")]
    if j.mode == "i2d":
        steps.append(("precheck", "Check image files"))
    if j.layout and j.layout.kind == "disk":
        steps.append(("table", "Write partition table"))
    steps += [("copy", "Copy data"), ("sync", "Flush to disk"), ("verify", "Verify target")]
    if j.method == "raw":
        steps.append(("gpt", "Fix GPT backup header"))
    return steps


class JobFailed(Exception):
    pass




class Alert(str):
    """A progress value that must be shown highlighted (for example unreadable bytes > 0)."""


class JobSink:
    """Receives the events of a running job. All methods are called from the job thread, so
    implementations must be thread-safe (marshal to their UI thread / pipe)."""

    def log(self, markup: str) -> None:            # one log line, Rich markup ($error, $warning...)
        pass

    def step(self, key: str, state: str) -> None:  # state: pending run done skip fail warn
        pass

    def item(self, iid: str, cells: dict) -> None:  # item table row (created on first use)
        pass

    def bar(self, which: str, pct: float, info: str | None = None) -> None:   # "copy" | "verify"
        pass

    def pairs(self, pairs: list[tuple[str, str, bool]]) -> None:   # (label, value, highlighted)
        pass

    def phase(self, text: str) -> None:
        pass

    def button(self, name: str, enabled: bool) -> None:   # "skip" | "stop"
        pass

    def begin(self, info: dict) -> None:   # next job of a batch starts (see JobRunner.start_info)
        pass


class JobRunner:
    """Runs one job (backup, restore, clone, check, browse, file backup). User-interface free:
    progress goes to a JobSink; stop/skip requests come from the UI thread."""

    def __init__(self, job: Job, sink: JobSink | None = None):
        self.job = job
        self.sink = sink or JobSink()
        self.proc: subprocess.Popen | None = None
        self.cur: Transfer | None = None
        self.stop_requested = False
        self.quit_after = False          # stop requested because the program is quitting
        self.skip_verify = threading.Event()
        self.steps = {k: "pending" for k, _ in job_steps(job)}
        self.step_names = dict(job_steps(job))
        if not job.verify and "verify" in self.steps and job.engine != "check":
            self.steps["verify"] = "skip"
        if "gpt" in self.steps and not job.fix_gpt:
            self.steps["gpt"] = "skip"
        if "precheck" in self.steps and not job.precheck:
            self.steps["precheck"] = "skip"
        self.stats: dict[str, str] = {}
        self.phase = ""
        self.locks: list[DevLock] = []
        self.inhibitor: subprocess.Popen | None = None
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.logpath = LOG_DIR / f"{ts}_{job.mode}.log"
        self.pclog = LOG_DIR / f"{ts}_{job.mode}_partclone.log"
        self._logf = None
        self._log_lock = threading.Lock()
        self.crypto: Crypto | None = None
        self.bad_total = 0
        self.cancel_ev = CondFlag(lambda: self.stop_requested)
        # progress bookkeeping for container jobs
        self.w_total = 1
        self.w_done = 0
        self.phase_t0 = time.monotonic()
        self.browse_session: BrowseSession | None = None
        self.result: tuple[str, str] | None = None
        self.running = False
        self.nas_src = self.nas_dst = None
        if job.src and job.src_dev is None:
            self.nas_src = net_share_of(existing_ancestor(Path(job.src)))
        if job.dst and job.dst_dev is None and job.mode != "brw":
            self.nas_dst = net_share_of(existing_ancestor(Path(job.dst)))
        # live progress: re-sent every second, also while a write blocks (see _ticker)
        self._live = None
        self._live_t = 0.0
        self._live_lock = threading.Lock()

    # ---- information for the UI ----
    def info_lines(self) -> list[str]:
        j = self.job
        info = [f"[b]{MODES[j.mode][0]}[/b]   [dim]{escape(engine_label(j.mode, j.method, j.compression, j.encrypt, j.manifest is not None))}[/]",
                f"[dim]from[/] {escape(j.src_label)}"]
        if self.nas_src:
            info.append(f"[dim]    [/] [b $accent]NAS[/] the backup is read from the network share "
                        f"{escape(self.nas_src)}")
        if j.mode != "chk":
            info.append(f"[dim]to  [/] {escape(j.dst_label)}")
        if self.nas_dst:
            info.append(f"[dim]    [/] [b $accent]NAS[/] the backup is written to the network share "
                        f"{escape(self.nas_dst)}")
        if j.engine not in ("files", "files-check"):
            info.append(f"[dim]size[/] {human(j.size)} ({j.size:,} B)")
        if j.mapfile:
            info.append(f"[dim]map [/] {escape(str(j.mapfile))}")
        info.append(f"[dim]log [/] {escape(str(self.logpath))}")
        return info

    def has_items_table(self) -> bool:
        return self.job.engine in ("container", "check", "browse")

    def start_info(self) -> dict:
        """What a user interface needs to show the progress page of this job."""
        return {"info": self.info_lines(), "steps": [[k, self.step_names[k], self.steps[k]] for k in self.step_names],
                "items_table": self.has_items_table(), "engine": self.job.engine, "mode": self.job.mode,
                "logpath": str(self.logpath)}

    def stop_text(self) -> str | None:
        """Text for the 'Stop?' question, or None when there is nothing to stop right now."""
        j = self.job
        if not self.running:
            return None
        if j.engine == "ddrescue":
            if not self.proc or self.proc.poll() is not None:
                return None
            return ("ddrescue will save its mapfile. You can resume the job later by choosing the "
                    "same source and target again.")
        if j.engine == "files":
            return ("The file backup will be stopped. No new version is saved; the repository stays "
                    "consistent and the next backup continues normally.")
        if j.engine == "files-check":
            return "The check will be stopped. Nothing was changed."
        return ("The job will be cancelled. It cannot be resumed; "
                + ("the incomplete backup folder stays marked as incomplete."
                   if j.mode == "d2i" else "the target will be left in an unusable state."
                   if j.mode != "chk" else "nothing was written."))

    # ---- requests from the UI thread ----
    def request_stop(self) -> None:
        if self.stop_requested:
            return
        self.stop_requested = True
        self.skip_verify.set()
        p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)
        if self.cur:
            self.cur.abort()
        self.log("[b $warning]Stop requested...[/]")

    def request_quit(self) -> None:
        """Stop because the program exits: also skip the remaining steps after the copy."""
        self.quit_after = True
        self.request_stop()

    def request_skip(self) -> bool:
        if self.steps.get("verify") == "run" or self.steps.get("precheck") == "run":
            self.skip_verify.set()
            if self.cur:
                self.cur.abort()
            self.log("[$warning]Skipping...[/]")
            return True
        return False

    # ---- event helpers (job thread) ----
    def log(self, msg: str) -> None:
        with self._log_lock:
            if self._logf:
                try:
                    self._logf.write(f"{datetime.now():%H:%M:%S} {strip_markup(msg)}\n")
                except OSError:
                    pass
        self.sink.log(msg)

    def set_step(self, key: str, state: str) -> None:
        if state != "run":
            self.set_live(None)         # the progress of a finished step stays as it is
        if key in self.steps:
            self.steps[key] = state
            self.sink.step(key, state)

    def item_row(self, iid: str, cells: dict) -> None:
        self.sink.item(iid, {k: str(v) for k, v in cells.items()})

    def pairs(self, pairs: list) -> None:
        self.sink.pairs([(str(k), str(v), isinstance(v, Alert)) for k, v in pairs])

    # ---- live progress ----
    def set_live(self, fn) -> None:
        """fn() re-sends the current progress; the ticker calls it when nothing else did for a
        second (a write to a NAS can block for many seconds; the clock keeps running anyway)."""
        with self._live_lock:
            self._live = fn

    def _ticker(self) -> None:
        while self.running:
            time.sleep(0.5)
            with self._live_lock:
                fn = self._live
                if fn is None or time.monotonic() - self._live_t < 0.95:
                    continue
                self._live_t = time.monotonic()
                try:
                    fn()
                except Exception:  # noqa: BLE001 - progress display must never break a job
                    pass

    def emit_progress(self, fn) -> None:
        """Run fn (which sends progress) serialized with the ticker."""
        with self._live_lock:
            self._live_t = time.monotonic()
            fn()

    def push_ddr_stats(self) -> None:
        with self._live_lock:
            self._live_t = time.monotonic()
            self._push_ddr_stats()

    def _push_ddr_stats(self) -> None:
        pairs = []
        t0 = getattr(self, "_ddr_t0", None)
        for key, label in DDR_KEYS:
            val = self.stats.get(key, "-")
            if key == "run time" and t0 is not None:
                val = fmt_dur(time.monotonic() - t0)
            pairs.append((label, val, key in ("bad-sector", "bad areas", "read errors")
                          and val not in ("-", "0", "0 B")))
        self.sink.pairs(pairs)
        try:
            pct = float(self.stats.get("pct rescued", "0").rstrip("%"))
        except ValueError:
            pct = 0.0
        self.sink.bar("copy", pct)
        self.sink.phase(self.phase)

    # ---- main entry (job thread) ----
    def run(self) -> tuple[str, str]:
        j = self.job
        self.running = True
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._logf = open(self.logpath, "a", buffering=1)
        except OSError:
            self._logf = None
        result, style = "ERROR", "$error"
        threading.Thread(target=self._ticker, daemon=True, name="diskimg-ticker").start()
        try:
            self._inhibit()
            if j.engine == "ddrescue":
                result, style = self._run_ddrescue(j)
            elif j.engine == "check":
                result, style = self._run_check(j)
            elif j.engine == "browse":
                result, style = self._run_browse(j)
            elif j.engine == "files":
                result, style = self._run_files(j)
            elif j.engine == "files-check":
                result, style = self._run_files_check(j)
            elif j.mode == "d2i":
                result, style = self._run_backup(j)
            else:
                result, style = self._run_write(j)
        except JobFailed as e:
            self.log(f"[b $error]{escape(str(e))}[/]")
            result = "STOPPED" if self.stop_requested else f"FAILED - {e}"
            style = "$warning" if self.stop_requested else "$error"
        except Exception as e:  # noqa: BLE001
            import traceback
            self.log(f"[b $error]Unexpected error: {escape(repr(e))}[/]")
            with self._log_lock:
                if self._logf:
                    self._logf.write(traceback.format_exc())
        finally:
            self.set_live(None)
            self.proc = None
            self.cur = None
            for lk in self.locks:
                lk.release()
            if self.crypto:
                self.crypto.close()
            if self.inhibitor and self.inhibitor.poll() is None:
                self.inhibitor.terminate()
            if j.dst_dev and j.dst_dev.kind != "part" and j.engine != "check":
                run(["blockdev", "--rereadpt", j.dst], timeout=30)
                run(["partx", "-u", j.dst], timeout=30)
                run(["partx", "-a", j.dst], timeout=30)
                if shutil.which("udevadm"):
                    run(["udevadm", "settle", "-t", "10"], timeout=20)
        self.result = (result, style)
        self.log(f"[b {style}]Result: {escape(result)}[/]")
        with self._log_lock:
            if self._logf:
                self._logf.close()
                self._logf = None
        self.running = False
        return result, style

    def _inhibit(self) -> None:
        if shutil.which("systemd-inhibit"):
            try:
                self.inhibitor = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", "--who=diskimg",
                     "--why=Disk imaging in progress", "--mode=block", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                self.inhibitor = None

    def lock_dev(self, path: str) -> None:
        lk = DevLock(path)
        try:
            lk.acquire()
        except OSError as e:
            raise JobFailed(f"Cannot lock {path} ({e.strerror}). Is it mounted or in use?")
        self.locks.append(lk)

    def flush(self, path: str) -> None:
        err = fsync_path(path)
        os.sync()
        if err:
            raise JobFailed(f"Flushing to disk failed: {err}")

    # ======================================================================
    #  ddrescue engine (raw .img, raw disk-to-disk)
    # ======================================================================
    def _run_ddrescue(self, j: Job) -> tuple[str, str]:
        MAP_DIR.mkdir(parents=True, exist_ok=True)
        for d in (j.src_dev, j.dst_dev):
            if d:
                self.lock_dev(d.path)
        old_img = Path(j.dst + ".old") if not j.dst_is_dev else None
        if not j.resume:
            if not j.dst_is_dev and j.replace_existing and Path(j.dst).exists():
                if old_img.exists():
                    # an earlier replacement did not finish: NAME.old is still the last good
                    # backup and NAME is the unfinished new one
                    Path(j.dst).unlink()
                    Path(j.dst + ".sha256").unlink(missing_ok=True)
                else:
                    # keep the previous backup until the new one is complete and verified
                    os.rename(j.dst, old_img)
                    for suf in (".sha256", ".map"):
                        if Path(j.dst + suf).exists():
                            os.rename(j.dst + suf, str(old_img) + suf)
                    self.log(f"Previous backup kept as {escape(str(old_img))} until the new one is "
                              "verified.")
            for p in (j.mapfile, Path(str(j.mapfile) + ".bak")):
                p.unlink(missing_ok=True)
            if not j.dst_is_dev:
                Path(j.dst + ".sha256").unlink(missing_ok=True)
        elif not j.dst_is_dev:
            # resuming: the checksum belongs to the earlier, unfinished state
            Path(j.dst + ".sha256").unlink(missing_ok=True)
        if j.dst_is_dev and not j.resume:
            # A larger target may still carry a GPT backup header / RAID or LVM signature of its
            # previous use at its end, outside the copied range: clear the last MiB.
            tsize = dev_size(j.dst)
            if tsize > j.size:
                tail = max(j.size, tsize - MIB)
                zero_range(j.dst, tail, tsize - tail)

        self.set_step("copy", "run")
        cmd = ["ddrescue", f"--retry-passes={j.retries}"]
        if j.dst_is_dev:
            cmd.append("--force")
        if j.sparse:
            cmd.append("--sparse")
        if j.idirect:
            cmd.append("--idirect")
        if j.resume:
            cmd.append("--verify-input-size")
        cmd += [j.src, j.dst, str(j.mapfile)]
        self.log(f"[b]$ {escape(' '.join(cmd))}[/]")
        rc = self.run_ddrescue_proc(cmd)
        if self.stop_requested:
            self.set_step("copy", "warn")
            self.log(f"[$warning]Copy stopped by user (ddrescue exit {rc}). "
                      f"Resume later with the same source and target.[/]")
            return "STOPPED - resumable", "$warning"
        if rc != 0:
            self.set_step("copy", "fail")
            self.log(f"[b $error]ddrescue failed with exit code {rc}.[/]")
            return f"FAILED - ddrescue exit {rc}", "$error"

        blocks = parse_mapfile(j.mapfile)
        totals = map_totals(blocks)
        bad = sum(v for k, v in totals.items() if k != "+")
        rescued = totals.get("+", 0)
        self.log(f"Rescued {human(rescued)} ({rescued:,} B) of {human(j.size)}.")
        if bad:
            self.log(f"[b $error]{human(bad)} ({bad:,} B) could not be read.[/] "
                      f"Details: " + ", ".join(f"{k}={v:,}" for k, v in totals.items() if k != "+"))
            self.set_step("copy", "warn")
        else:
            self.set_step("copy", "done")

        if bad and j.zero_fill:
            self.set_step("fill", "run")
            zf = STATE_DIR / "zero.bin"
            if not zf.exists() or zf.stat().st_size != 65536:
                zf.write_bytes(b"\0" * 65536)
            fcmd = ["ddrescue", "--fill-mode=?*/-", "--force", str(zf), j.dst, str(j.mapfile)]
            self.log(f"[b]$ {escape(' '.join(fcmd))}[/]")
            rc = self.run_ddrescue_proc(fcmd)          # registered: Stop interrupts it
            if self.stop_requested:
                self.set_step("fill", "warn")
                return "STOPPED - zero-fill not finished", "$warning"
            if rc == 0:
                self.set_step("fill", "done")
                self.log("Unreadable areas on the target were overwritten with zeros.")
            else:
                self.set_step("fill", "fail")
                self.log(f"[$error]Zero-fill failed (ddrescue exit {rc}).[/]")
        else:
            self.set_step("fill", "skip")

        self.set_step("sync", "run")
        self.flush(j.dst)
        self.set_step("sync", "done")

        verify_state = "skipped"
        if j.verify:
            self.set_step("verify", "run")
            self.sink.button("skip", True)
            self.sink.button("stop", False)
            verify_state = self.verify_raw(j, rescued_regions(blocks), full=(bad == 0))
            self.sink.button("skip", False)
            self.set_step("verify", {"ok": "done", "skipped": "skip"}.get(verify_state, "fail"))
        if self.quit_after:
            return "STOPPED", "$warning"

        gpt_note = self.fix_gpt(j)

        if old_img is not None and old_img.exists():
            if verify_state == "ok" and not bad:
                for suf in ("", ".sha256", ".map"):
                    Path(str(old_img) + suf).unlink(missing_ok=True)
                self.log("The previous backup was replaced.")
            else:
                self.log(f"[b $warning]The new image is not verified or not complete, so the previous "
                          f"backup was kept as {escape(str(old_img))}.[/]")

        img_bad = 0
        if j.mode == "i2d":
            # the image itself may contain zeros where the original disk was unreadable
            img_bad = image_unreadable_bytes(j.src)
            if img_bad:
                self.log(f"[b $warning]The image records {human(img_bad)} that were unreadable when "
                          "it was made; those areas are zeros on the target.[/]")
        if verify_state == "mismatch":
            return "VERIFICATION FAILED - target differs from source", "$error"
        if verify_state == "error":
            return "VERIFICATION ERROR - see log", "$error"
        if bad:
            return f"DONE WITH {human(bad)} UNREADABLE{gpt_note}", "$warning"
        if img_bad:
            return f"DONE - image had {human(img_bad)} unreadable at backup time{gpt_note}", "$warning"
        if gpt_note:
            return f"DONE - copied, but the GPT backup header could not be fixed{gpt_note}", "$warning"
        if verify_state == "ok":
            return "SUCCESS - verified bit-exact", "$success"
        return f"DONE - not verified{gpt_note}", "$warning"

    def fix_gpt(self, j: Job) -> str:
        if "gpt" not in self.steps:
            return ""
        if j.fix_gpt and j.dst_dev and j.dst_size > j.size and has_gpt(j.dst):
            self.set_step("gpt", "run")
            r = run(["sgdisk", "-e", j.dst], timeout=120)
            self.log(escape((r.stdout + r.stderr).strip() or "sgdisk -e done"))
            if r.returncode == 0:
                self.set_step("gpt", "done")
                self.log("GPT backup header moved to the end of the larger target. "
                          "The extra space is unallocated.")
                self.flush(j.dst)
                return ""
            self.set_step("gpt", "fail")
            return " (GPT fix failed)"
        self.set_step("gpt", "skip")
        return ""

    def run_ddrescue_proc(self, cmd: list[str]) -> int:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                     close_fds=True, start_new_session=True)
        _reg(self.proc)
        os.close(slave)
        ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        buf = ""
        last_push = 0.0
        self._ddr_t0 = time.monotonic()
        self.set_live(self._push_ddr_stats)
        try:
            while True:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data.decode(errors="replace")
                parts = re.split(r"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    self.handle_ddr_line(ansi.sub("", p).strip())
                now = time.monotonic()
                if now - last_push > 0.25:
                    last_push = now
                    self.push_ddr_stats()
            if buf.strip():
                self.handle_ddr_line(ansi.sub("", buf).strip())
        finally:
            rc = self.proc.wait()
            _unreg(self.proc)
            os.close(master)
            self.set_live(None)
            self.push_ddr_stats()
            self._ddr_t0 = None
        return rc

    def handle_ddr_line(self, s: str) -> None:
        if not s:
            return
        low = s.lower()
        if any(k in low for k in ("rescued:", "ipos:", "opos:", "non-tried:", "pct rescued:",
                                  "time since last")):
            for seg in s.split(","):
                if ":" in seg:
                    k, v = seg.split(":", 1)
                    self.stats[k.strip().lower()] = v.strip()
            return
        if s.startswith(("GNU ddrescue", "Press Ctrl-C")):
            return
        if s.startswith("ddrescue:"):
            self.log(f"[$error]{escape(s)}[/]")
            return
        if s != self.phase:
            self.phase = s
            if not s.startswith("Initial status"):
                self.log(f"[dim]{escape(s)}[/]")

    def verify_raw(self, j: Job, regions: list[tuple[int, int]], full: bool) -> str:
        total = sum(sz for _, sz in regions)
        if total == 0:
            self.log("[$warning]Nothing was rescued, nothing to verify.[/]")
            return "skipped"
        self.log(f"Verifying {human(total)} ({'whole device' if full else 'readable areas only'}) "
                  "by reading back source and target and comparing byte by byte...")
        chunk = 8 * MIB
        sha = hashlib.sha256() if full else None
        mismatches: list[int] = []
        errors: list[str] = []
        done = 0
        t0 = time.monotonic()
        last = 0.0
        for path in (j.src, j.dst):
            err = drop_cache(path)
            if err:
                self.log(f"[$error]Cannot prepare the verification: {escape(err)}[/]")
                return "error"
        try:
            fs = os.open(j.src, os.O_RDONLY)
            fd = os.open(j.dst, os.O_RDONLY)
        except OSError as e:
            self.log(f"[$error]Cannot open for verification: {escape(str(e))}[/]")
            return "error"
        try:
            for f in (fs, fd):
                try:
                    os.posix_fadvise(f, 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass

            def jobs():
                for pos, sz in regions:
                    off, end = pos, pos + sz
                    while off < end:
                        n = min(chunk, end - off)
                        yield off, n
                        off += n

            def rd(fdx, n, off):
                data = os.pread(fdx, n, off)
                try:
                    os.posix_fadvise(fdx, off, n, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
                return data

            with ThreadPoolExecutor(max_workers=4) as ex:
                it = jobs()
                pending = []
                for _ in range(2):
                    nxt = next(it, None)
                    if nxt:
                        pending.append((nxt, ex.submit(rd, fs, nxt[1], nxt[0]),
                                        ex.submit(rd, fd, nxt[1], nxt[0])))
                while pending:
                    if self.skip_verify.is_set():
                        for _, a, b in pending:
                            a.cancel()
                            b.cancel()
                        self.log("[$warning]Verification skipped by user.[/]")
                        return "skipped"
                    (off, n), fa, fb = pending.pop(0)
                    nxt = next(it, None)
                    if nxt:
                        pending.append((nxt, ex.submit(rd, fs, nxt[1], nxt[0]),
                                        ex.submit(rd, fd, nxt[1], nxt[0])))
                    try:
                        a, b = fa.result(), fb.result()
                    except OSError as e:
                        errors.append(f"read error at {off:#x}: {e.strerror}")
                        sha = None
                        done += n
                        continue
                    if len(a) != n or len(b) != n:
                        # one side ends early (truncated image / smaller device): never "equal"
                        errors.append(f"short read at {off:#x}: source {len(a)}, target {len(b)} "
                                      f"of {n} bytes")
                        sha = None
                        done += n
                        continue
                    if a != b:
                        if len(a) != len(b):
                            mismatches.append(off + min(len(a), len(b)))
                        else:
                            for k in range(0, n, 4096):
                                if a[k:k + 4096] != b[k:k + 4096]:
                                    mismatches.append(off + k)
                                    break
                    if sha is not None:
                        sha.update(b)
                    done += n
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        rate = done / max(now - t0, 1e-6)
                        msg = (f"{human(done)} / {human(total)}   {human(rate)}/s   "
                               f"mismatches: {len(mismatches)}   read errors: {len(errors)}")
                        self.sink.bar("verify", done * 100 / total, msg)
        finally:
            os.close(fs)
            os.close(fd)

        elapsed = time.monotonic() - t0
        msg = (f"{human(done)} checked in {elapsed:.0f}s   mismatches: {len(mismatches)}   "
               f"read errors: {len(errors)}")
        self.sink.bar("verify", 100, msg)
        for e in errors[:10]:
            self.log(f"[$error]{escape(e)}[/]")
        if mismatches:
            shown = ", ".join(f"{m:#x}" for m in mismatches[:10])
            self.log(f"[b $error]MISMATCH in {len(mismatches)} chunk(s). First offsets: {shown}[/]")
            return "mismatch"
        if errors:
            return "error"
        self.log("[b $success]Verification passed: target is identical to the source "
                  f"({'whole range' if full else 'all readable areas'}).[/]")
        if sha is not None:
            digest = sha.hexdigest()
            self.log(f"SHA-256: {digest}")
            if j.mode == "d2i":
                Path(j.dst + ".sha256").write_text(f"{digest}  {Path(j.dst).name}\n")
                self.log(f"Checksum saved to {j.dst}.sha256")
            elif j.mode == "i2d":
                cf = Path(j.src + ".sha256")
                if cf.exists():
                    stored = cf.read_text().split()[0].lower()
                    if stored == digest:
                        self.log("[$success]Image matches the checksum recorded at backup time.[/]")
                    else:
                        self.log("[b $error]Image does NOT match the checksum recorded at backup "
                                  "time. The image file was modified or is corrupt.[/]")
                        return "mismatch"
        return "ok"

    # ======================================================================
    #  container engine helpers
    # ======================================================================
    def xfer(self, t: Transfer, label: str, weight: int, bar: str) -> bool:
        """Run a transfer with live progress. Returns True on success."""
        self.cur = t
        if self.stop_requested or (bar == "verify" and self.skip_verify.is_set()):
            t.cancel.set()

        meter = RateMeter()
        packing = self.job.mode == "d2i" and bool(self.job.compression)

        def send(tr: Transfer):
            frac = max(0.0, min(1.0, tr.fraction()))
            overall = (self.w_done + weight * frac) / max(self.w_total, 1)
            el = time.monotonic() - self.phase_t0
            rate = meter.rate(tr.bytes_in)
            eta = el * (1 - overall) / overall if overall > 0.02 else None
            bad = self.bad_total + tr.src.bad_bytes()
            if bar == "copy":
                pairs = [("Item", label), ("Item progress", f"{frac * 100:5.1f}%"),
                         ("Total", f"{overall * 100:5.1f}%"),
                         ("Elapsed", fmt_dur(el)), ("Remaining", fmt_dur(eta)),
                         ("Rate", f"{human(rate)}/s"),
                         ("Read", human(tr.bytes_in)), ("Written", human(tr.bytes_out))]
                if packing:
                    pairs.append(("Compression", ratio_text(tr.bytes_in, tr.bytes_out)))
                pairs.append(("Unreadable", Alert(human(bad)) if bad else "0 B"))
                self.pairs(pairs)
                self.sink.bar("copy", overall * 100)
            else:
                self.sink.bar("verify", overall * 100,
                        f"{label}: {frac * 100:.1f}%   {human(rate)}/s   elapsed {fmt_dur(el)}   "
                        f"remaining {fmt_dur(eta)}")

        def prog(tr: Transfer):
            self.emit_progress(lambda: send(tr))
        self.set_live(lambda: send(t))
        try:
            ok = t.run(on_progress=prog)
        finally:
            self.set_live(None)
        self.cur = None
        self.w_done += weight
        if not t.cancelled:
            for e in t.errors:
                self.log(f"[$error]{escape(e)}[/]")
        return ok

    def start_phase(self, weights: int) -> None:
        self.w_total = max(weights, 1)
        self.w_done = 0
        self.phase_t0 = time.monotonic()

    def check_stop(self) -> None:
        if self.stop_requested:
            raise JobFailed("stopped by user")

    @staticmethod
    def item_weight(it: dict) -> int:
        return max(int(it.get("length") or it.get("stream_bytes") or 1), 1)

    def _items_table(self, items: list[dict], nodes: dict | None = None) -> None:
        for it in items:
            dev = ""
            if nodes is not None:
                dev = nodes.get(it["id"], "")
            self.item_row(it["id"], {
                "Item": it["id"], "Device": dev, "Size": human(it.get("length")),
                "FS": it.get("fstype") or "-",
                "Method": it.get("tool") or it.get("method"),
                "Copy": "pending" if it.get("method") not in ("skip",) else "skipped",
                "Verify": "-"})

    # ---- verification of container files (used by backup verify and check) ----
    def verify_container_files(self, cdir: Path, man: dict, decode: bool, bar: str = "verify",
                               step: str = "verify") -> str:
        items = [i for i in man.get("items", []) if i.get("file")]
        if man.get("encryption") and decode and not self.crypto:
            self.crypto = Crypto(self.job.passphrase or "")
        self.start_phase(sum(max(int(i.get("file_bytes") or 1), 1) for i in items))
        bad = 0
        for it in items:
            if self.skip_verify.is_set():
                self.log("[$warning]Skipped by user.[/]")
                return "skipped"
            fpath = cdir / it["file"]
            col = "Verify" if step == "verify" else "Copy"
            if not fpath.is_file():
                self.log(f"[b $error]{it['file']} is missing.[/]")
                self.item_row(it["id"], {col: "MISSING"})
                bad += 1
                continue
            self.item_row(it["id"], {col: "checking"})
            hf = Hasher()
            hs = Hasher(partclone=it.get("method") == "partclone")
            if it.get("method") == "chunks":
                try:
                    probs = self._verify_chunked_files(cdir, it, decode, col)
                except InterruptedError:
                    if self.skip_verify.is_set() and not self.stop_requested:
                        self.log("[$warning]Skipped by user.[/]")
                        return "skipped"
                    raise JobFailed("stopped by user")
                if probs:
                    bad += 1
                    self.item_row(it["id"], {col: "BAD"})
                    self.log(f"[b $error]{it['id']} ({it['file']}): {escape('; '.join(probs))}[/]")
                else:
                    self.item_row(it["id"], {col: "ok"})
                continue
            filters = decode_filters(man, self.crypto, it) if decode else []
            t = Transfer(FileSrc(fpath), NullDst(), filters, hf, hs if decode else None)
            ok = self.xfer(t, it["id"], max(int(it.get("file_bytes") or 1), 1), bar)
            if t.cancelled:
                if self.skip_verify.is_set() and not self.stop_requested:
                    self.log("[$warning]Skipped by user.[/]")
                    return "skipped"
                raise JobFailed("stopped by user")
            problems = []
            if not ok:
                problems.append("decode error")
            if hf.n != it.get("file_bytes") or hf.hexdigest() != it.get("file_sha256"):
                problems.append("file checksum mismatch")
            if decode and ok and (hs.n != it.get("stream_bytes") or hs.hexdigest() != it.get("stream_sha256")):
                problems.append("data checksum mismatch")
            if problems:
                bad += 1
                self.item_row(it["id"], {col: "BAD"})
                self.log(f"[b $error]{it['id']} ({it['file']}): {', '.join(problems)}[/]")
            else:
                self.item_row(it["id"], {col: "ok"})
        if bad:
            return "mismatch"
        self.log("[b $success]All image files are intact" + (" and decode to exactly the data "
                  "that was read from the source." if decode else ".") + "[/]")
        return "ok"

    # ======================================================================
    #  container: backup (disk -> .dimg)
    # ======================================================================
    def make_live_src(self, it: dict, part: PartInfo | None, lay: Layout, src_disk: str) -> Src:
        role = it["role"]
        if role == "boot":
            return RawSrc(src_disk, 0, it["length"], rescue=True)
        if role == "ntfsbak":
            return RawSrc(part.node, it["offset"], it["length"], rescue=True)
        if it["method"] == "partclone":
            return CmdSrc([it["tool"], "-c", "-R", "-s", part.node, "-o", "-", "-L", str(self.pclog)])
        return RawSrc(part.node, 0, part.size, rescue=True)

    def plan_items(self, lay: Layout, ext: str, chunked: bool = False) -> list[dict]:
        items: list[dict] = []
        if lay.kind == "disk":
            items.append({"id": "boot", "role": "boot", "part": None, "method": "blob", "tool": "",
                          "fstype": lay.label, "offset": 0, "length": lay.boot_len,
                          "file": f"bootarea.bin{ext}"})
            if chunked:
                items[-1]["codec"] = "plain"
        for p in lay.parts:
            base = {"part": p.number, "fstype": p.fstype, "offset": 0, "length": p.size}
            if chunked and p.method in ("partclone", "raw"):
                name = "disk" if (lay.kind == "single" and p.number is None and self.job.src_dev
                                  and self.job.src_dev.kind != "part") else p.tag
                mapped = p.method == "partclone"
                stem = f"{p.tag}.{p.fstype}" if mapped else f"{name}.raw"
                items.append({**base, "id": p.tag, "role": "part", "method": "chunks", "codec": "chunks",
                              "mapped": mapped, "tool": p.tool if mapped else "",
                              "file": f"{stem}.chunks", "index_file": f"{stem}.cidx",
                              "extra_regions": [p.ntfs_bak] if (mapped and p.ntfs_bak) else []})
                continue
            if p.method == "skip":
                items.append({**base, "id": p.tag, "role": "part", "method": "skip", "tool": "",
                              "file": None})
            elif p.method == "swap":
                items.append({**base, "id": p.tag, "role": "part", "method": "swap", "tool": "mkswap",
                              "file": None, "uuid": p.uuid, "label": p.label})
            elif p.method == "partclone":
                items.append({**base, "id": p.tag, "role": "part", "method": "partclone",
                              "tool": p.tool, "file": f"{p.tag}.{p.fstype}.pcl{ext}"})
                if p.ntfs_bak:
                    items.append({"id": f"{p.tag}-bootbak", "role": "ntfsbak", "part": p.number,
                                  "method": "blob", "tool": "", "fstype": "ntfs",
                                  "offset": p.ntfs_bak[0], "length": p.ntfs_bak[1],
                                  "file": f"{p.tag}.ntfs-bootbak.bin{ext}"})
            else:
                name = "disk" if (lay.kind == "single" and p.number is None and
                                  self.job.src_dev and self.job.src_dev.kind != "part") else p.tag
                items.append({**base, "id": p.tag, "role": "part", "method": "raw", "tool": "",
                              "file": f"{name}.raw{ext}"})
        return items

    def _run_backup(self, j: Job) -> tuple[str, str]:
        self.set_step("prep", "run")
        src_disk = j.src_dev.path
        self.lock_dev(src_disk)
        lay = analyze(j.src_dev, j.method, j.allow_btrfs)     # fresh, under lock
        for line in layout_lines(lay):
            self.log(escape(line))
        for w in lay.warnings:
            self.log(f"[$warning]{escape(w)}[/]")
        final = Path(j.dst)
        if final.exists() and not j.replace_existing:
            raise JobFailed(f"{final} already exists.")
        # The new backup is written to NAME.partial. An existing backup is only replaced when the
        # new one is complete, so a failed run never leaves you without any backup.
        out = final.with_name(final.name + ".partial")
        try:
            remove_container(out)
        except ValueError as e:
            raise JobFailed(str(e))
        out.mkdir()
        if j.encrypt:
            self.crypto = Crypto(j.passphrase or "")
        chunked = not j.encrypt
        ext = "" if chunked else (".zst" if j.compression else "") + (".gpg" if j.encrypt else "")
        filters = [] if chunked else encode_filters(j.compression, self.crypto)
        sd = j.src_dev
        man = {
            "format": "diskimg-container", "version": 3 if chunked else 2, "tool": f"diskimg {VERSION}",
            "chunked": chunked,
            "created": datetime.now().isoformat(timespec="seconds"),
            "complete": False, "verified": None,
            "type": j.method,
            "compression": {"algo": "zstd", "level": j.compression} if j.compression else None,
            "encryption": {"algo": "gpg-symmetric", "cipher": "AES256"} if j.encrypt else None,
            "source": {"path": sd.path, "kind": sd.kind, "size": lay.size, "model": sd.model,
                       "serial": sd.serial, "sector_size": lay.sector_size},
            "partclone": run(["partclone.ntfs", "-v"], timeout=10).stdout.strip()[:80]
            if shutil.which("partclone.ntfs") else None,
            "layout": lay.to_json(),
            "items": [],
        }
        if lay.sfdisk_dump:
            (out / "ptable.sfdisk").write_text(lay.sfdisk_dump)
            if lay.label == "gpt":
                run(["sgdisk", f"--backup={out / 'ptable.sgdisk'}", src_disk], timeout=60)
        items = self.plan_items(lay, ext, chunked)
        man["items"] = items
        save_manifest(out, man)
        parts_by_num = {p.number: p for p in lay.parts}
        self._items_table(items, {it["id"]: (src_disk if it["role"] == "boot"
                                             else parts_by_num[it["part"]].node) for it in items})
        self.set_step("prep", "done")

        # ---- copy ----
        self.set_step("copy", "run")
        self.start_phase(sum(self.item_weight(i) for i in items if i.get("file")))
        warn = False
        for it in items:
            self.check_stop()
            if not it.get("file"):
                if it["method"] == "swap":
                    self.item_row(it["id"], {"Copy": "uuid saved"})
                continue
            part = parts_by_num.get(it["part"]) if it["role"] != "boot" else None
            if it["method"] == "chunks":
                ok, info = self._backup_chunked(it, part, out, j.compression)
                if not ok and it.get("mapped") and info == "map" and j.fallback_raw \
                        and not self.stop_requested:
                    self.log(f"[$warning]{it['id']}: filesystem-aware copy failed. Copying the whole "
                              f"partition raw instead.[/]")
                    it.update(mapped=False, tool="", file=f"{it['id']}.raw.chunks",
                              index_file=f"{it['id']}.raw.cidx", extra_regions=[])
                    ok, info = self._backup_chunked(it, part, out, j.compression)
                if not ok:
                    self.item_row(it["id"], {"Copy": "FAILED"})
                    save_manifest(out, man)
                    raise JobFailed(f"{it['id']} could not be copied.")
                if it.get("bad_bytes"):
                    warn = True
                save_manifest(out, man)
                continue
            ok, info = self._backup_item(it, part, lay, src_disk, out, filters, ext)
            if not ok and it["method"] == "partclone" and j.fallback_raw and not self.stop_requested \
                    and info == "src":
                self.log(f"[$warning]{it['id']}: filesystem-aware copy failed. Copying the whole "
                          f"partition raw instead.[/]")
                (out / it["file"]).unlink(missing_ok=True)
                it.update(method="raw", tool="", file=f"{it['id']}.raw{ext}", stream_sha256=None)
                it.pop("payload_sha256", None)
                for other in items:
                    if other["role"] == "ntfsbak" and other["part"] == it["part"]:
                        other["role"], other["file"], other["method"] = "dropped", None, "skip"
                        self.item_row(other["id"], {"Copy": "in raw copy"})
                self.w_done -= self.item_weight(it)
                ok, info = self._backup_item(it, part, lay, src_disk, out, filters, ext)
            if not ok:
                self.item_row(it["id"], {"Copy": "FAILED"})
                save_manifest(out, man)
                raise JobFailed(f"{it['id']} could not be copied.")
            if it.get("bad_bytes"):
                warn = True
            save_manifest(out, man)
        self.set_step("copy", "warn" if warn else "done")

        # ---- manifest ----
        self.set_step("meta", "run")
        man["items"] = [i for i in items if i["role"] != "dropped"]
        man["complete"] = True
        man["bad_bytes"] = self.bad_total
        save_manifest(out, man)
        with open(out / "SHA256SUMS", "w") as f:
            for i in man["items"]:
                if i.get("file"):
                    f.write(f"{i['file_sha256']}  {i['file']}\n")
                if i.get("index_file"):
                    f.write(f"{i['index_sha256']}  {i['index_file']}\n")
        self.write_info(out, man, lay)
        os.sync()
        self.set_step("meta", "done")
        total_files = sum(i.get("file_bytes") or 0 for i in man["items"])
        self.log(f"Backup size on disk: {human(total_files)}"
                  + (f" (source data {human(sum(i.get('stream_bytes') or 0 for i in man['items']))})"
                     if j.compression else ""))

        # ---- verify ----
        vstate = "skipped"
        if j.verify:
            self.set_step("verify", "run")
            self.sink.button("skip", True)
            vstate = self.verify_container_files(out, man, decode=True)
            self.sink.button("skip", False)
            self.set_step("verify", {"ok": "done", "skipped": "skip"}.get(vstate, "fail"))
            if vstate == "ok":
                man["verified"] = datetime.now().isoformat(timespec="seconds")
                save_manifest(out, man)
        if vstate == "mismatch":
            self.log(f"[b $error]The damaged new backup was left in {escape(str(out))}."
                      + (f" The previous backup {escape(str(final))} was not touched." if final.exists()
                         else "") + "[/]")
            return "VERIFICATION FAILED - backup is damaged", "$error"
        self.finalize_backup(out, final, clean=(vstate == "ok" and not self.bad_total))
        if self.bad_total:
            return f"DONE WITH {human(self.bad_total)} UNREADABLE (zeros in backup)", "$warning"
        if vstate == "ok":
            return "SUCCESS - backup written and verified", "$success"
        return "DONE - not verified", "$warning"

    def finalize_backup(self, work: Path, final: Path, clean: bool) -> None:
        """Move the finished backup from NAME.partial to NAME. A previous backup becomes NAME.old
        and is deleted only when the new one is verified without any unreadable data."""
        old = final.with_name(final.name + ".old")
        try:
            if final.exists():
                if old.exists():
                    remove_container(old)
                os.rename(final, old)
            os.rename(work, final)
            fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError) as e:
            raise JobFailed(f"Could not move the new backup into place: {e}. It is in {work}.")
        if old.exists():
            if clean:
                try:
                    remove_container(old)
                    self.log("The previous backup was replaced.")
                except (OSError, ValueError) as e:
                    self.log(f"[$warning]Could not delete the previous backup {escape(str(old))}: "
                              f"{escape(str(e))}[/]")
            else:
                self.log(f"[b $warning]The new backup is not verified or has unreadable areas, so the "
                          f"previous backup was kept as {escape(str(old))}. Delete it yourself when "
                          f"you no longer need it.[/]")

    def _backup_item(self, it, part, lay, src_disk, out, filters, ext) -> tuple[bool, str]:
        self.item_row(it["id"], {"Copy": "running", "Method": it.get("tool") or it["method"]})
        src = self.make_live_src(it, part, lay, src_disk)
        hs = Hasher(partclone=it["method"] == "partclone")
        hf = Hasher()
        t = Transfer(src, FileDst(out / it["file"]), filters, hs, hf if filters else None)
        ok = self.xfer(t, it["id"], self.item_weight(it), "copy")
        if t.cancelled:
            raise JobFailed("stopped by user")
        hf_final = hf if filters else hs
        it.update(stream_bytes=hs.n, stream_sha256=hs.hexdigest(),
                  file_bytes=hf_final.n, file_sha256=hf_final.hexdigest(),
                  bad_bytes=src.bad_bytes(), bad_ranges=src.bad_ranges()[:1000])
        if hs.payload is not None:
            it["payload_sha256"] = hs.payload_hex()
        if not ok:
            origin = "src" if t.src_error and not t.dst_error and not t.filter_errors else "other"
            return False, origin
        if src.bad_bytes():
            self.bad_total += src.bad_bytes()
            self.log(f"[b $error]{it['id']}: {human(src.bad_bytes())} unreadable, stored as zeros.[/]")
            self.item_row(it["id"], {"Copy": f"BAD {human(src.bad_bytes())}"})
        else:
            ratio = ""
            if it.get("stream_bytes") and filters:
                ratio = f" -> {human(it['file_bytes'])}"
            self.item_row(it["id"], {"Copy": f"ok {human(it['stream_bytes'])}{ratio}"})
        return True, ""

    def write_info(self, out: Path, man: dict, lay: Layout) -> None:
        lines = [f"diskimg {VERSION} backup", f"created: {man['created']}",
                 f"source: {man['source']['path']} {man['source'].get('model') or ''} "
                 f"{man['source'].get('serial') or ''} {man['source']['size']:,} bytes",
                 f"type: {'filesystem-aware (partclone)' if man['type'] == 'fs' else 'raw'}",
                 f"compression: {'zstd' if man['compression'] else 'none'}",
                 f"encryption: {'gpg AES-256' if man['encryption'] else 'none'}", ""]
        lines += layout_lines(lay, 200)
        lines += ["", "Files:"]
        for i in man["items"]:
            if i.get("file"):
                lines.append(f"  {i['file']:<40} {i['role']:<8} {i.get('tool') or i['method']}")
        if man.get("chunked"):
            lines += ["", "Format: chunked (instant browse). Each *.chunks file holds independently",
                      "zstd-compressed 4 MiB blocks; *.cidx is the index (header: magic DIMGIDX1, chunk size,",
                      "total size, count; per chunk: offset u64, stored size u32, plain size u32,",
                      "flag u8 [0 unused, 1 zstd, 2 zeros, 3 stored], blake2b-128 hash).",
                      "Rebuild a partition image without the menu:",
                      "  diskimg --extract pN.fs.chunks pN.fs.cidx partition.img",
                      "then: dd if=partition.img of=/dev/TARGETPART bs=4M conv=fsync",
                      "(do NOT add conv=sparse: it skips zero blocks and leaves old data on the target)",
                      "or mount it read-only: mount -o loop,ro partition.img /mnt"]
        lines += ["", "Restore with diskimg (mode 'Image -> Disk'). Manual restore of one item, e.g.:"]
        dec = []
        if man["encryption"]:
            dec.append("gpg -d FILE")
        if man["compression"]:
            dec.append("zstd -dc" + ("" if dec else " FILE"))
        pre = " | ".join(dec) if dec else "cat FILE"
        lines += [f"  partclone image:  {pre} | partclone.<fs> -r -s - -o /dev/TARGETPART",
                  f"  raw image:        {pre} | dd of=/dev/TARGET bs=4M conv=fsync",
                  "  The boot area (bootarea.bin*) goes to the start of the target disk; for GPT run "
                  "'sgdisk -e' afterwards.",
                  "  ptable.sfdisk can be applied with: sfdisk /dev/TARGET < ptable.sfdisk",
                  "Check the files with: sha256sum -c SHA256SUMS"]
        (out / "info.txt").write_text("\n".join(lines) + "\n")

    # ======================================================================
    #  container: write to a device (restore .dimg, or fs-aware disk clone)
    # ======================================================================
    def _run_write(self, j: Job) -> tuple[str, str]:
        self.set_step("prep", "run")
        clone = j.mode == "d2d"
        target = j.dst
        if clone:
            self.lock_dev(j.src_dev.path)
            lay = analyze(j.src_dev, j.method, j.allow_btrfs)
            items = self.plan_items(lay, "")
            man = None
            cdir = None
        else:
            cdir = Path(j.src)
            man = load_manifest(cdir)
            if not man.get("complete"):
                raise JobFailed("The backup is incomplete.")
            lay = Layout.from_json(man["layout"])
            items = [dict(i) for i in man["items"]]
            if man.get("encryption"):
                self.crypto = Crypto(j.passphrase or "")
                if not test_passphrase(cdir, man, j.passphrase or ""):
                    raise JobFailed("Wrong passphrase.")
        self.lock_dev(target)
        tsize = dev_size(target)
        sp = None
        shrink_dir = None
        if tsize < lay.needed:
            if clone or j.method != "fs":
                raise JobFailed(f"Target too small: needs {human(lay.needed)}, has {human(tsize)}.")
            try:
                sp = shrink_plan(lay, items, tsize, cdir)
                shrink_dir = shrink_tmp_dir(cdir, sp.tmp_need)
            except ValueError as e:
                raise JobFailed(f"Target too small: needs {human(lay.needed)}, has {human(tsize)}. {e}")
            for line in shrink_lines(sp):
                self.log(line)
            self._table_changed = True
        if lay.kind == "disk":
            if j.dst_dev.kind == "part":
                raise JobFailed("A whole-disk backup needs a whole disk as target.")
            if sector_size(target) != lay.sector_size:
                raise JobFailed("Logical sector size of the target differs from the source.")
        for line in layout_lines(lay):
            self.log(escape(line))
        self._items_table(items)
        parts_by_num = {p.number: p for p in lay.parts}
        self.set_step("prep", "done")

        # ---- pre-check of image files ----
        if not clone:
            if j.precheck:
                self.set_step("precheck", "run")
                self.sink.button("skip", True)
                st = self.verify_container_files(cdir, man, decode=False, step="precheck")
                self.sink.button("skip", False)
                if st == "mismatch":
                    self.set_step("precheck", "fail")
                    raise JobFailed("The image files are damaged. Nothing was written to the target.")
                self.set_step("precheck", "done" if st == "ok" else "skip")
                self.sink.bar("verify", 0, "image files checked - target verification follows the copy"
                        if st == "ok" else "image check skipped")
                self.skip_verify.clear()
                for it in items:
                    if it.get("file"):
                        self.item_row(it["id"], {"Copy": "pending"})

        def source_for(it: dict) -> tuple[Src, list]:
            if clone:
                part = parts_by_num.get(it["part"]) if it["role"] != "boot" else None
                return self.make_live_src(it, part, lay, j.src_dev.path), []
            return FileSrc(cdir / it["file"]), decode_filters(man, self.crypto, it)

        # ---- partition table ----
        nodes: dict[int | None, str] = {None: target}
        if lay.kind == "disk":
            self.set_step("table", "run")
            self.check_stop()
            nodes = self.write_table(j, lay, items, source_for, man, cdir, sp)
            self.set_step("table", "done")
            for it in items:
                if it.get("part") is not None:
                    self.item_row(it["id"], {"Device": nodes.get(it["part"], "?")})

        # ---- data ----
        self.set_step("copy", "run")
        data_items = [i for i in items if i["role"] in ("part", "ntfsbak")]
        self.start_phase(sum(self.item_weight(i) for i in data_items if i["method"] != "skip"))
        warn = False
        for it in data_items:
            self.check_stop()
            node = nodes.get(it["part"]) if lay.kind == "disk" else target
            if it["method"] == "skip" or it.get("_done"):
                continue
            if sp is not None and it["role"] == "part" and it["part"] == sp.part:
                self._restore_shrunk(j, it, node, sp, lay, items, source_for, cdir, shrink_dir, target)
                continue
            if it["method"] == "swap":
                cmd = ["mkswap", "-U", it.get("uuid") or "random"]
                if it.get("label"):
                    cmd += ["-L", it["label"]]
                cmd.append(node)
                with self.unlocked(target):
                    r = run(cmd, timeout=120)
                if r.returncode != 0:
                    raise JobFailed(f"mkswap failed: {r.stderr.strip()}")
                self.item_row(it["id"], {"Copy": "recreated"})
                continue
            part = parts_by_num.get(it["part"])
            psize = part.size if lay.kind == "disk" else dev_size(node)
            if it["method"] == "chunks":
                if not self._restore_chunked(it, node, psize, cdir):
                    self.item_row(it["id"], {"Copy": "FAILED"})
                    raise JobFailed(f"{it['id']} could not be written. The target is incomplete.")
                continue
            ok = self._write_item(j, it, node, psize, source_for, clone)
            if not ok and clone and it["method"] == "partclone" and j.fallback_raw \
                    and not self.stop_requested and it.get("_srcfail"):
                self.log(f"[$warning]{it['id']}: filesystem-aware copy failed. Copying the whole "
                          f"partition raw instead.[/]")
                it.update(method="raw", tool="")
                for other in data_items:
                    if other["role"] == "ntfsbak" and other["part"] == it["part"]:
                        other["method"] = "skip"
                        self.item_row(other["id"], {"Copy": "in raw copy"})
                self.w_done -= self.item_weight(it)
                ok = self._write_item(j, it, node, psize, source_for, clone)
            if not ok:
                self.item_row(it["id"], {"Copy": "FAILED"})
                raise JobFailed(f"{it['id']} could not be written. The target is incomplete.")
            if it.get("bad_bytes"):
                warn = True
        self.set_step("copy", "warn" if warn else "done")

        self.set_step("sync", "run")
        errs = [e for e in (fsync_path(n) for n in set(nodes.values()) | {target}) if e]
        os.sync()
        if errs:
            self.set_step("sync", "fail")
            raise JobFailed("Flushing to the target failed (write error): " + "; ".join(errs))
        self.set_step("sync", "done")

        # ---- verify ----
        vstate = "skipped"
        if j.verify:
            self.set_step("verify", "run")
            self.sink.button("skip", True)
            # read back from the disk itself, not from the page cache
            errs = [e for e in (drop_cache(n) for n in set(nodes.values()) | {target}) if e]
            if errs:
                self.set_step("verify", "fail")
                raise JobFailed("Cannot prepare the verification: " + "; ".join(errs))
            vstate = self.verify_target(j, lay, items, nodes)
            self.sink.button("skip", False)
            self.set_step("verify", {"ok": "done", "skipped": "skip"}.get(vstate, "fail"))
        if self.quit_after:
            return "STOPPED", "$warning"
        if sp is not None:
            # NTFS partitions that moved forward: their boot sector records the partition start
            for p in lay.parts:
                newstart = sp.new_parts.get(p.number, [p.start])[0]
                if p.fstype == "ntfs" and newstart != p.start and p.number in nodes:
                    try:
                        patch_ntfs_start(nodes[p.number], newstart // lay.sector_size)
                        self.log(f"Partition {p.number} moved: NTFS start position updated.")
                    except OSError as e:
                        self.log(f"[$warning]Partition {p.number}: NTFS start position not updated ({e}).[/]")

        gpt_note = ""
        if j.method == "raw":
            j.size = lay.size
            gpt_note = self.fix_gpt(j)

        bad = self.bad_total + sum(int(i.get("bad_bytes") or 0) for i in items if not clone)
        if vstate == "mismatch":
            return "VERIFICATION FAILED - target differs", "$error"
        if vstate == "error":
            return "VERIFICATION ERROR - see log", "$error"
        if bad:
            return f"DONE - {human(bad)} were unreadable at backup/copy time{gpt_note}", "$warning"
        if gpt_note:
            return f"DONE - written, but the GPT backup header could not be fixed{gpt_note}", "$warning"
        if vstate == "ok":
            return "SUCCESS - written and verified", "$success"
        return f"DONE - not verified{gpt_note}", "$warning"

    @contextmanager
    def unlocked(self, path: str):
        lk = next((x for x in self.locks if x.path == path), None)
        if lk is None:
            yield
            return
        try:
            with lk.released():
                yield
        except OSError as e:
            if e.errno == errno.EBUSY and "lock" in str(e):
                raise JobFailed(f"{e}. Another program opened the target meanwhile.")
            raise

    def write_table(self, j: Job, lay: Layout, items, source_for, man, cdir, sp: ShrinkPlan | None = None) -> dict:
        target = j.dst
        tsize = dev_size(target)
        boot = next(i for i in items if i["role"] == "boot")
        pos = {p.number: tuple(sp.new_parts[p.number]) if sp else (p.start, p.size) for p in lay.parts}
        end = max(st + sz for st, sz in pos.values())
        # remove stale GPT backup / signatures at the end of the target
        tail_start = max(end, tsize - MIB)
        zero_range(target, tail_start, tsize - tail_start)
        # boot area (MBR / protective MBR + primary GPT + boot code)
        src, filters = source_for(boot)
        hb = Hasher()
        t = Transfer(src, RawDst(target, 0, boot["length"]), filters, Hasher(), hb if filters else None)
        if not t.run():
            for e in t.errors:
                self.log(f"[$error]{escape(e)}[/]")
            raise JobFailed("Writing the boot area failed.")
        hstream = hb if filters else t.hA
        if boot.get("stream_sha256") and hstream.hexdigest() != boot["stream_sha256"]:
            raise JobFailed("Boot area in the image is damaged (checksum mismatch).")
        boot["stream_sha256"] = hstream.hexdigest()
        boot["stream_bytes"] = hstream.n
        self.item_row("boot", {"Device": target, "Copy": f"ok {human(boot['length'])}"})
        self.log(f"Boot area written ({human(boot['length'])}).")
        if sp is not None:
            # new layout: the saved table with the new positions, written by sfdisk; the boot code
            # in the first 440 bytes stays as restored
            dump = lay.sfdisk_dump if j.mode == "d2d" else (cdir / "ptable.sfdisk").read_text()
            dump = adjust_sfdisk_dump(dump, sp.new_parts, lay.sector_size)
            fd = os.open(target, os.O_RDWR | os.O_CLOEXEC)
            try:
                code = os.pread(fd, 440, 0)
            finally:
                os.close(fd)
            r = run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", "--wipe", "never", target],
                    timeout=120, inp=dump)
            if r.returncode != 0:
                raise JobFailed(f"sfdisk failed: {(r.stdout + r.stderr).strip()}")
            fd = os.open(target, os.O_RDWR | os.O_CLOEXEC)
            try:
                pwrite_all(fd, code, 0)
                os.fsync(fd)
            finally:
                os.close(fd)
            v = run(["sfdisk", "--verify", target], timeout=120)
            if v.returncode != 0:
                raise JobFailed(f"Partition table check failed: {(v.stdout + v.stderr).strip()}")
            self.log("Partition table written with the new layout (sfdisk --verify: ok).")
        elif lay.label == "gpt":
            r = run(["sgdisk", "-e", target], timeout=120)
            if r.returncode != 0:
                raise JobFailed(f"sgdisk -e failed: {(r.stdout + r.stderr).strip()}")
            v = run(["sgdisk", "-v", target], timeout=120)
            if "No problems found" not in v.stdout:
                raise JobFailed(f"GPT check failed: {v.stdout.strip()}")
            self.log("GPT backup header written to the end of the target. sgdisk -v: no problems.")
        elif any((p.number or 0) >= 5 or p.method == "skip" for p in lay.parts):
            dump = lay.sfdisk_dump if j.mode == "d2d" else (cdir / "ptable.sfdisk").read_text()
            r = run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", "--wipe", "never", target],
                    timeout=120, inp=dump)
            if r.returncode != 0:
                raise JobFailed(f"sfdisk failed: {(r.stdout + r.stderr).strip()}")
            self.log("MBR logical partitions written with sfdisk.")
        fsync_path(target)
        expect = {p.number for p in lay.parts if p.method != "skip"}
        parts = reread_partitions(target, expect)
        nodes: dict[int | None, str] = {}
        for p in lay.parts:
            if p.method == "skip":
                continue
            if p.number not in parts:
                raise JobFailed(f"Partition {p.number} did not appear on the target after writing the "
                                f"partition table.")
            node, start, size = parts[p.number]
            want_start, want_size = pos[p.number]
            if start != want_start or size != want_size:
                raise JobFailed(f"{node}: start/size differ from the planned layout "
                                f"({start}/{size} vs {want_start}/{want_size}).")
            nodes[p.number] = node
        self.log("Partitions on target: " + ", ".join(f"{n}={v}" for n, v in sorted(nodes.items())))
        return nodes

    def _restore_shrunk(self, j: Job, it: dict, node: str, sp: ShrinkPlan, lay: Layout, items: list[dict],
                        source_for, cdir: Path, tmpdir: Path, target: str) -> None:
        """Restore the partition that is made smaller (see shrink_plan) and shrink its filesystem."""
        self._table_changed = True
        tail = sp.old_size - sp.new_size
        tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tf = tmpdir / f"diskimg-shrink-{os.getpid()}.tail"
        fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.ftruncate(fd, tail)
        finally:
            os.close(fd)
        loop = None
        name = f"diskimg-shrink-{os.getpid()}"
        self.log(f"{it['id']}: restoring at the original size {human(sp.old_size)}; the last "
                 f"{human(tail)} are kept in {tf} for now.")
        try:
            r = run(["losetup", "-f", "--show", str(tf)], timeout=60)
            if r.returncode != 0:
                raise JobFailed(f"losetup failed: {(r.stderr or r.stdout).strip()}")
            loop = r.stdout.strip()
            table = (f"0 {sp.new_size // 512} linear {node} 0\n"
                     f"{sp.new_size // 512} {tail // 512} linear {loop} 0\n")
            with self.unlocked(target):           # device-mapper claims the partition itself
                r = run(["dmsetup", "create", name], timeout=60, inp=table)
                if r.returncode != 0:
                    raise JobFailed(f"dmsetup failed: {(r.stderr or r.stdout).strip()}")
                dm = f"/dev/mapper/{name}"
                try:
                    if it["method"] == "chunks":
                        if not self._restore_chunked(it, dm, sp.old_size, cdir):
                            raise JobFailed(f"{it['id']} could not be written. The target is incomplete.")
                    elif not self._write_item(j, it, dm, sp.old_size, source_for, False):
                        raise JobFailed(f"{it['id']} could not be written. The target is incomplete.")
                    for nb in items:
                        if nb["role"] == "ntfsbak" and nb.get("part") == sp.part and nb["method"] != "skip":
                            if not self._write_item(j, nb, dm, sp.old_size, source_for, False):
                                raise JobFailed(f"{nb['id']} could not be written.")
                            nb["_done"] = True
                            nb["_verified"] = (True, "")   # rewritten by the resize
                    err = fsync_path(dm)
                    if err:
                        raise JobFailed(f"Flushing failed: {err}")
                    if j.verify:
                        self.item_row(it["id"], {"Verify": "checking"})
                        drop_cache(dm)
                        res, why = self._verify_item(j, lay, it, {sp.part: dm}, target)
                        if res is False:
                            it["_verified"] = (False, why)
                            raise JobFailed(f"{it['id']}: the restored data differs from the backup ({why}).")
                        it["_verified"] = (True, "") if res is True else ("skipped", "")
                        self.item_row(it["id"], {"Verify": "ok" if res is True else "skipped"})
                    self.item_row(it["id"], {"Copy": "making smaller"})
                    try:
                        shrink_filesystem(sp.fstype, dm, sp.new_size, self.log)
                    except ValueError as e:
                        raise JobFailed(f"{it['id']}: {e}")
                    err = fsync_path(dm)
                    if err:
                        raise JobFailed(f"Flushing failed: {err}")
                finally:
                    run(["dmsetup", "remove", "--retry", name], timeout=120)
        finally:
            if loop:
                run(["losetup", "-d", loop], timeout=60)
            try:
                tf.unlink()
            except OSError:
                pass
            try:
                tmpdir.rmdir()
            except OSError:
                pass
        drop_cache(node)
        prob = check_filesystem(sp.fstype, node)
        if prob:
            raise JobFailed(f"{it['id']}: the smaller filesystem does not check out: {prob}")
        self.item_row(it["id"], {"Copy": f"ok, made smaller to {human(sp.new_size)}"})
        self.log(f"[$success]{it['id']}: restored and made smaller to {human(sp.new_size)}; filesystem check ok.[/]")

    def _write_item(self, j: Job, it: dict, node: str, psize: int, source_for, clone: bool) -> bool:
        self.item_row(it["id"], {"Copy": "running", "Device": node,
                                 "Method": it.get("tool") or it["method"]})
        src, filters = source_for(it)
        pc = it["method"] == "partclone"
        hs = Hasher(partclone=pc)
        if it["role"] == "ntfsbak":
            dst: Dst = RawDst(node, it["offset"], it["length"])
        elif pc:
            # wipe stale signatures at both ends, then let partclone write the used blocks
            zero_range(node, 0, min(MIB, psize))
            zero_range(node, max(psize - MIB, MIB), max(min(MIB, psize - MIB), 0))
            dst = CmdDst([it["tool"], "-r", "-s", "-", "-o", node, "-L", str(self.pclog)])
        else:
            dst = RawDst(node, 0, psize)
        hA = Hasher(partclone=pc) if clone else Hasher()
        t = Transfer(src, dst, filters, hA, hs if filters else None)
        ok = self.xfer(t, it["id"], self.item_weight(it), "copy")
        if t.cancelled:
            raise JobFailed("stopped by user")
        stream = hs if filters else hA
        if not ok:
            it["_srcfail"] = bool(clone and t.src_error)
            return False
        if clone:
            it.update(stream_bytes=stream.n, stream_sha256=stream.hexdigest(),
                      bad_bytes=src.bad_bytes())
            if pc:
                it["payload_sha256"] = stream.payload_hex()
            if src.bad_bytes():
                self.bad_total += src.bad_bytes()
                self.log(f"[b $error]{it['id']}: {human(src.bad_bytes())} unreadable on the source, "
                          f"written as zeros.[/]")
        else:
            if stream.n != it.get("stream_bytes") or stream.hexdigest() != it.get("stream_sha256"):
                self.item_row(it["id"], {"Copy": "BAD DATA"})
                raise JobFailed(f"{it['id']}: data from the image does not match its checksum. "
                                "The image is damaged.")
        if it.get("bad_bytes"):
            self.item_row(it["id"], {"Copy": f"ok, {human(it['bad_bytes'])} zeros"})
        else:
            self.item_row(it["id"], {"Copy": f"ok {human(stream.n)}"})
        return True

    def verify_target(self, j: Job, lay: Layout, items: list[dict], nodes: dict) -> str:
        target = j.dst
        todo = [i for i in items if i["method"] not in ("skip",) and i["role"] != "dropped"]
        self.start_phase(sum(self.item_weight(i) for i in todo))
        bad = 0
        for it in todo:
            if self.skip_verify.is_set():
                self.log("[$warning]Verification skipped by user.[/]")
                return "skipped"
            self.check_stop()
            self.item_row(it["id"], {"Verify": "checking"})
            if "_verified" in it:                 # checked before it was made smaller
                res, why = it["_verified"]
            else:
                res, why = self._verify_item(j, lay, it, nodes, target)
            if res == "skipped":
                return "skipped"
            self.item_row(it["id"], {"Verify": "ok" if res else "BAD"})
            if not res:
                bad += 1
                self.log(f"[b $error]{it['id']}: {escape(why)}[/]")
        if bad:
            return "mismatch"
        self.log("[b $success]Verification passed: everything written to the target was read back "
                  "and matches " + ("the source" if j.mode == "d2d" else "the image") + ".[/]")
        return "ok"

    def _verify_item(self, j, lay, it, nodes, target) -> tuple[bool | str, str]:
        node = nodes.get(it.get("part")) if lay.kind == "disk" else target
        w = self.item_weight(it)
        if it["role"] == "boot" and getattr(self, "_table_changed", False):
            # new layout: compare the boot code, and check the rewritten partition table
            self.w_done += w
            src_boot = self.boot_bytes(j, it)
            with open(target, "rb") as f:
                got = f.read(440)
            v = run(["sfdisk", "--verify", target], timeout=120)
            ok = src_boot is not None and got == src_boot[:440] and v.returncode == 0
            return ok, "boot code or new partition table differs"
        if it["role"] == "boot":
            want_len = it["length"]
            # GPT: sector 1 (header) and the protective MBR size field change with the disk size
            if lay.label == "gpt":
                ss = lay.sector_size
                with open(target, "rb") as f:
                    got = f.read(want_len)
                src_boot = self.boot_bytes(j, it)
                self.w_done += w
                if src_boot is None:
                    return False, "the boot area of the backup could not be read for comparison"
                if len(got) != want_len or len(src_boot) != want_len:
                    return False, "boot area has the wrong length"
                same = (got[:440] == src_boot[:440] and got[2 * ss:] == src_boot[2 * ss:])
                v = run(["sgdisk", "-v", target], timeout=120)
                return (same and "No problems found" in v.stdout), "boot area / GPT differs"
            t = Transfer(RawSrc(target, 0, want_len, rescue=False), NullDst())
            ok = self.xfer(t, it["id"], w, "verify")
            if t.cancelled:
                return "skipped", ""
            return ok and t.hA.hexdigest() == it.get("stream_sha256"), "boot area differs"
        if it["method"] == "chunks":
            return self._verify_chunked_target(it, node, Path(j.src))
        if it["method"] == "swap":
            self.w_done += w
            got = blkid_probe(node)
            return (got.get("TYPE") == "swap" and (not it.get("uuid") or got.get("UUID") == it["uuid"]),
                    "swap signature/UUID differs")
        if it["method"] == "partclone":
            h = Hasher(partclone=True)
            t = Transfer(CmdSrc([it["tool"], "-c", "-s", node, "-o", "-", "-L", str(self.pclog)]),
                         NullDst(), (), h)
            ok = self.xfer(t, it["id"], w, "verify")
            if t.cancelled:
                return "skipped", ""
            if not ok:
                return False, "partclone could not read the written filesystem"
            return h.payload_hex() == it.get("payload_sha256"), "used blocks differ"
        # raw and ntfs backup boot sector: read back exactly what was written
        off = it["offset"] if it["role"] == "ntfsbak" else 0
        h = Hasher()
        t = Transfer(RawSrc(node, off, int(it["stream_bytes"]), rescue=False), NullDst(), (), h)
        ok = self.xfer(t, it["id"], w, "verify")
        if t.cancelled:
            return "skipped", ""
        return ok and h.hexdigest() == it.get("stream_sha256"), "data read back differs"

    def boot_bytes(self, j: Job, it: dict) -> bytes | None:
        if j.mode == "d2d":
            with open(j.src_dev.path, "rb") as f:
                return f.read(it["length"])
        man = load_manifest(Path(j.src))
        dst = BytesDst()
        t = Transfer(FileSrc(Path(j.src) / it["file"]), dst, decode_filters(man, self.crypto, it))
        return bytes(dst.buf) if t.run() else None

    # ======================================================================
    #  chunked format (v3)
    # ======================================================================
    def chunk_progress(self, label: str, weight: int, bar: str):
        """Progress callback for the chunked engine: prog(done, total, bad=0, written=None,
        packed_in=None); written/packed_in (backups) = bytes stored so far / data they hold."""
        last = [0.0]
        meter = RateMeter()
        state: list = [0, 1, 0, None, None]

        def send() -> None:
            done, total, bad, written, packed_in = state
            now = time.monotonic()
            frac = max(0.0, min(1.0, done / max(total, 1)))
            overall = (self.w_done + weight * frac) / max(self.w_total, 1)
            el = now - self.phase_t0
            rate = meter.rate(done) if total > 1000 else 0
            eta = el * (1 - overall) / overall if overall > 0.02 else None
            badt = self.bad_total + bad
            if bar == "copy":
                pairs = [
                    ("Item", label), ("Item progress", f"{frac * 100:5.1f}%"),
                    ("Total", f"{overall * 100:5.1f}%"),
                    ("Elapsed", fmt_dur(el)), ("Remaining", fmt_dur(eta)),
                    ("Rate", f"{human(rate)}/s" if total > 1000 else "-"),
                    ("Processed", (f"{human(done)} of {human(total)}") if total > 1000
                     else f"{done}/{total} chunks")]
                if written is not None:
                    pairs.append(("Written", human(written)))
                    pairs.append(("Compression", ratio_text(packed_in or 0, written)))
                pairs.append(("Unreadable", Alert(human(badt)) if badt else "0 B"))
                self.pairs(pairs)
                self.sink.bar("copy", overall * 100)
            else:
                self.sink.bar("verify", overall * 100,
                        f"{label}: {frac * 100:.1f}%   elapsed {fmt_dur(el)}   remaining {fmt_dur(eta)}")

        def prog(done: int, total: int, bad: int = 0, written: int | None = None,
                 packed_in: int | None = None) -> None:
            state[:] = [done, total, bad, written, packed_in]
            now = time.monotonic()
            if now - last[0] < 0.3 and done < total:
                return
            last[0] = now
            self.emit_progress(send)
        self.set_live(send)
        return prog

    def _backup_chunked(self, it: dict, part: PartInfo, out: Path, level: int) -> tuple[bool, str]:
        self.item_row(it["id"], {"Copy": "reading map" if it.get("mapped") else "running",
                                 "Method": (it["tool"] + " map") if it.get("mapped") else "raw"})
        node, size = part.node, part.size
        if it.get("mapped"):
            try:
                regs = domain_regions(it["tool"], node, self.pclog, cancel=self.cancel_ev)
            except InterruptedError:
                raise JobFailed("stopped by user")
            except (ValueError, OSError, subprocess.TimeoutExpired) as e:
                self.log(f"[$error]{it['id']}: {escape(str(e))}[/]")
                return False, "map"
            regs += [tuple(x) for x in it.get("extra_regions") or []]
        else:
            regs = [(0, size)]
        used = sum(n for _, n in merge_regions(regs, size))
        self.log(f"{it['id']}: {human(used)} of {human(size)} to copy "
                  f"({used * 100 / max(size, 1):.0f}% of the partition)")
        self.item_row(it["id"], {"Copy": "running"})
        w = self.item_weight(it)
        try:
            st = chunked_backup(node, regs, size, level, out / it["file"], out / it["index_file"],
                                self.cancel_ev, self.chunk_progress(it["id"], w, "copy"))
        except InterruptedError:
            raise JobFailed("stopped by user")
        except OSError as e:
            self.log(f"[$error]{it['id']}: {escape(str(e))}[/]")
            return False, "other"
        self.w_done += w
        it.update(st)
        if st["bad_bytes"]:
            self.bad_total += st["bad_bytes"]
            self.log(f"[b $error]{it['id']}: {human(st['bad_bytes'])} unreadable, stored as zeros.[/]")
            self.item_row(it["id"], {"Copy": f"BAD {human(st['bad_bytes'])}"})
        else:
            self.item_row(it["id"], {"Copy": f"ok {human(st['stream_bytes'])} -> {human(st['file_bytes'])}"})
        return True, ""

    def _verify_chunked_files(self, cdir: Path, it: dict, decode: bool, col: str) -> list[str]:
        data, index = cdir / it["file"], cdir / it["index_file"]
        for f in (data, index):
            if not f.is_file():
                return [f"{f.name} is missing"]
        w = max(int(it.get("file_bytes") or 1), 1)
        if not decode:
            hd = Hasher()
            t = Transfer(FileSrc(data), NullDst(), (), hd)
            self.xfer(t, it["id"], w, "verify")
            if t.cancelled:
                raise InterruptedError("cancelled")
            probs = []
            if hd.hexdigest() != it.get("file_sha256"):
                probs.append("data file checksum mismatch")
            if hashlib.sha256(index.read_bytes()).hexdigest() != it.get("index_sha256"):
                probs.append("index checksum mismatch")
            return probs
        ev = CondFlag(lambda: self.skip_verify.is_set() or self.stop_requested)
        probs = chunked_verify_files(data, index, it, ev, self.chunk_progress(it["id"], w, "verify"))
        self.w_done += w
        if ev.is_set():
            raise InterruptedError("cancelled")
        return probs

    def _restore_chunked(self, it: dict, node: str, psize: int, cdir: Path) -> bool:
        self.item_row(it["id"], {"Copy": "running", "Device": node, "Method": "chunks"})
        if it.get("mapped"):
            zero_range(node, 0, min(MIB, psize))
            zero_range(node, max(psize - MIB, MIB), max(min(MIB, psize - MIB), 0))
        w = self.item_weight(it)
        try:
            chunked_restore(cdir / it["file"], cdir / it["index_file"], node, 0, psize, self.cancel_ev,
                            self.chunk_progress(it["id"], w, "copy"))
        except InterruptedError:
            raise JobFailed("stopped by user")
        except ChunkError as e:
            self.item_row(it["id"], {"Copy": "BAD DATA"})
            raise JobFailed(f"{it['id']}: the backup data is damaged ({e}). The target is incomplete.")
        except OSError as e:
            self.log(f"[$error]{it['id']}: write error: {escape(str(e))}[/]")
            return False
        self.w_done += w
        note = f", {human(it['bad_bytes'])} zeros" if it.get("bad_bytes") else ""
        self.item_row(it["id"], {"Copy": f"ok {human(it.get('stream_bytes') or 0)}{note}"})
        return True

    def _verify_chunked_target(self, it: dict, node: str, cdir: Path) -> tuple[bool | str, str]:
        ev = CondFlag(lambda: self.skip_verify.is_set() or self.stop_requested)
        w = self.item_weight(it)
        try:
            bad = chunked_verify_target(cdir / it["index_file"], node, 0, ev,
                                        self.chunk_progress(it["id"], w, "verify"))
        except InterruptedError:
            return "skipped", ""
        except OSError as e:
            return False, f"read error: {e}"
        self.w_done += w
        return (not bad), f"{len(bad)} chunk(s) differ (first: {bad[:3]})"

    def start_chunk_server(self, specs: list[str]) -> tuple[subprocess.Popen, Path]:
        rd = Path("/run/diskimg")
        rd.mkdir(mode=0o700, parents=True, exist_ok=True)
        fmp = rd / f"fuse-{os.getpid()}-{int(time.time() * 1000) % 100000000}"
        fmp.mkdir()
        logpath = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_fuse.log"
        with open(logpath, "a") as log:          # the child keeps its own copy of the descriptor
            p = subprocess.Popen([sys.executable, "-I", str(RUN_SCRIPT), "serve-chunks", str(fmp)] + specs,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                 start_new_session=True)
        _reg(p)
        import select
        r, _, _ = select.select([p.stdout], [], [], 30)
        line = p.stdout.readline().decode(errors="replace").strip() if r else ""
        if line != "READY":
            if p.poll() is None:
                p.kill()
            p.wait()
            _unreg(p)
            try:
                tail = logpath.read_text().strip().splitlines()[-1]
            except (OSError, IndexError):
                tail = "no output"
            try:
                fmp.rmdir()
            except OSError:
                pass
            raise JobFailed(f"The virtual disk service did not start: {tail}")
        return p, fmp

    # ======================================================================
    #  file backups (restic)
    # ======================================================================
    def _mount_win_source(self, fs: dict, mounted_here: list[str]) -> Path | None:
        uuid = fs.get("win_uuid")
        if not uuid or not fs.get("win_paths"):
            return None
        node = find_by_uuid(uuid)
        if not node:
            raise JobFailed(f"The Windows partition ({fs.get('win_label') or uuid}) is not connected.")
        mp = SRC_ROOT / sanitize(uuid)
        if os.path.ismount(mp):
            return mp
        mp.mkdir(parents=True, exist_ok=True)
        cur = run(["findmnt", "-n", "-o", "TARGET", "-S", node], timeout=10).stdout.strip().splitlines()
        if cur:
            r = run(["mount", "--bind", cur[0], str(mp)], timeout=30)
            if r.returncode == 0:
                run(["mount", "-o", "remount,bind,ro", str(mp)], timeout=30)
                mounted_here.append(str(mp))
                self.log(f"Windows partition is mounted at {cur[0]}; using it read-only via {mp}.")
                return mp
            raise JobFailed(f"Could not access the mounted Windows partition: {r.stderr.strip()}")
        err = mount_ro(node, "ntfs", str(mp))
        if err:
            raise JobFailed(f"Could not mount the Windows partition read-only: {err}")
        mounted_here.append(str(mp))
        self.log(f"Windows partition {node} mounted read-only at {mp}.")
        return mp

    def _run_files(self, j: Job) -> tuple[str, str]:
        fs = j.fileset
        base = restic_base(fs)
        mounted_here: list[str] = []
        self.set_step("prep", "run")
        paths: list[str] = []
        missing = [p for p in fs.get("linux", []) if not os.path.exists(p)]
        if missing:
            raise JobFailed("Folder(s) not found: " + ", ".join(missing))
        paths += fs.get("linux", [])
        try:
            wmp = self._mount_win_source(fs, mounted_here)
            if wmp is not None:
                for wp in fs.get("win_paths", []):
                    p = wmp / wp.lstrip("/")
                    if not p.exists():
                        raise JobFailed(f"Windows folder not found: {wp}")
                    paths.append(str(p))
            self.set_step("prep", "done")
            self.set_step("repo", "run")
            if not repo_exists(fs["repo"]):
                if fs.get("repo_initialized"):
                    raise JobFailed(f"The repository {fs['repo']} was not found. Is the NAS / disk "
                                    "connected? No new, empty repository is created in its place.")
                prob = repo_location_problem(fs["repo"])
                if prob:
                    raise JobFailed(prob)
                Path(fs["repo"]).mkdir(parents=True, exist_ok=True)
                r = run(base + ["init"], timeout=600)
                if r.returncode != 0:
                    raise JobFailed(f"restic init failed: {(r.stderr or r.stdout).strip()[-300:]}")
                self.log(f"New repository created in {fs['repo']}.")
                self._mark_repo_initialized(fs)
            else:
                r = run(base + ["cat", "config"], timeout=300)
                if r.returncode != 0:
                    raise JobFailed(f"Cannot open the repository: {(r.stderr or r.stdout).strip()[-300:]}")
                if not fs.get("repo_initialized"):
                    self._mark_repo_initialized(fs)
            self.set_step("repo", "done")
            self.set_step("backup", "run")
            cmd = base + ["backup", "--json", "--one-file-system", "--tag", f"diskimg-set:{fs['id']}",
                          "--host", socket.gethostname()]
            for e in fs.get("excludes", []):
                cmd += ["--exclude", e]
            cmd += paths
            self.log(f"[b]$ restic backup {escape(' '.join(paths))}[/]")
            rc, summary, errors = self._run_restic_json(cmd)
            if self.stop_requested:
                self.set_step("backup", "warn")
                return "STOPPED - no new version was saved", "$warning"
        finally:
            for mp in reversed(mounted_here):
                umount_path(mp, lazy=True)
        if rc not in (0, 3):
            self.set_step("backup", "fail")
            return f"FAILED - restic exit {rc} (see log)", "$error"
        if summary is None:
            self.set_step("backup", "warn")
            self.log("[$error]restic did not report a saved version.[/]")
            return "UNCERTAIN - restic reported no new version (see log)", "$warning"
        s = summary
        self.log(f"Version {str(s.get('snapshot_id', '?'))[:8]} saved: {s.get('files_new', 0)} new, "
                  f"{s.get('files_changed', 0)} changed, {s.get('files_unmodified', 0)} unchanged files; "
                  f"{human(s.get('data_added', 0))} added to the repository.")
        prune_note = self._apply_keep_policy(fs)
        if rc == 3:
            self.set_step("backup", "warn")
            return f"DONE - {errors} file(s) could not be read (see log){prune_note}", "$warning"
        if prune_note:
            self.set_step("backup", "warn")
            return f"DONE - backup saved{prune_note}", "$warning"
        self.set_step("backup", "done")
        return (f"SUCCESS - {s.get('files_new', 0)} new, {s.get('files_changed', 0)} changed, "
                f"{human(s.get('data_added', 0))} added"), "$success"

    def _apply_keep_policy(self, fs: dict) -> str:
        """Delete old versions according to the set's policy. Returns '' or a warning suffix."""
        args = KEEP_POLICIES.get(fs.get("keep") or "", ("", []))[1]
        if not args or self.stop_requested:
            return ""
        self.log(f"Removing old versions ({KEEP_POLICIES[fs['keep']][0]}) ...")
        r = run(restic_base(fs) + ["forget", "--tag", f"diskimg-set:{fs['id']}", "--group-by", "tags",
                                   "--prune"] + args, timeout=None)
        if r.returncode != 0:
            self.log(f"[$error]Removing old versions failed: "
                      f"{escape((r.stderr or r.stdout).strip()[-300:])}[/]")
            return ", but removing old versions failed"
        return ""

    @staticmethod
    def _mark_repo_initialized(fs: dict) -> None:
        """Remember that this set's repository exists, so a missing repository later (share not
        connected) is reported instead of silently creating a new one elsewhere."""
        fs["repo_initialized"] = True
        sets = load_filesets()
        for x in sets:
            if x.get("id") == fs["id"] and x.get("repo") == fs["repo"]:
                x["repo_initialized"] = True
        try:
            save_filesets(sets)
        except OSError:
            pass

    def _run_restic_json(self, cmd: list[str]) -> tuple[int, dict | None, int]:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
        _reg(self.proc)
        errtail: deque[str] = deque(maxlen=20)
        threading.Thread(target=lambda: [errtail.append(x.rstrip()) for x in self.proc.stderr],
                         daemon=True).start()
        summary = None
        nerr = 0
        t0 = time.monotonic()
        last = 0.0
        for line in self.proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            mt = m.get("message_type")
            if mt == "status":
                now = time.monotonic()
                if now - last < 0.3:
                    continue
                last = now
                pct = float(m.get("percent_done") or 0) * 100
                bd, tb = m.get("bytes_done", 0), m.get("total_bytes", 0)
                cur = (m.get("current_files") or [""])[0]
                rate = bd / max(now - t0, 1e-3)
                self.pairs([
                    ("Progress", f"{pct:5.1f}%"), ("Files", f"{m.get('files_done', 0):,} / "
                                                            f"{m.get('total_files', 0):,}"),
                    ("Elapsed", fmt_dur(now - t0)),
                    ("Data", f"{human(bd)} / {human(tb)}"), ("Rate", f"{human(rate)}/s"),
                    ("Remaining", fmt_dur(m.get("seconds_remaining"))),
                    ("Errors", Alert(str(nerr)) if nerr else "0"),
                    ("Current", escape(cur[-60:])), ("", "")])
                self.sink.bar("copy", pct)
            elif mt == "error":
                nerr += 1
                if nerr <= 30:
                    err = m.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    self.log(f"[$warning]{escape(str(m.get('item') or ''))}: {escape(str(msg))}[/]")
            elif mt == "summary":
                summary = m
                self.sink.bar("copy", 100)
                self.pairs([
                    ("Progress", "100%"), ("Files", f"{m.get('total_files_processed', 0):,}"),
                    ("Duration", fmt_dur(m.get("total_duration"))),
                    ("New files", f"{m.get('files_new', 0):,}"),
                    ("Changed", f"{m.get('files_changed', 0):,}"),
                    ("Unchanged", f"{m.get('files_unmodified', 0):,}"),
                    ("Processed", human(m.get("total_bytes_processed", 0))),
                    ("Added", human(m.get("data_added", 0))),
                    ("Errors", Alert(str(nerr)) if nerr else "0")])
        rc = self.proc.wait()
        _unreg(self.proc)
        if rc not in (0, 3) and not self.stop_requested:
            for x in list(errtail)[-6:]:
                self.log(f"[$error]{escape(x)}[/]")
        return rc, summary, nerr

    def _run_files_check(self, j: Job) -> tuple[str, str]:
        fs = j.fileset
        self.set_step("repo", "run")
        r = run(restic_base(fs) + ["cat", "config"], timeout=300)
        if r.returncode != 0:
            self.set_step("repo", "fail")
            raise JobFailed(f"Cannot open the repository: {(r.stderr or r.stdout).strip()[-300:]}")
        self.set_step("repo", "done")
        self.set_step("check", "run")
        cmd = restic_base(fs) + ["check"] + (["--read-data"] if j.verify else [])
        self.log(f"[b]$ restic check{' --read-data' if j.verify else ''}[/]")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)
        _reg(self.proc)
        for line in self.proc.stdout:
            line = line.rstrip()
            if line and not line.startswith("["):
                self.log(escape(line))
        rc = self.proc.wait()
        _unreg(self.proc)
        if self.stop_requested:
            return "STOPPED", "$warning"
        if rc == 0:
            self.set_step("check", "done")
            return ("OK - repository intact" + (" (all data read)" if j.verify else " (structure)"),
                    "$success")
        self.set_step("check", "fail")
        return f"PROBLEMS FOUND - restic check exit {rc} (see log)", "$error"

    # ======================================================================
    #  browse: unpack selected partitions and mount them read-only
    # ======================================================================
    def _run_browse(self, j: Job) -> tuple[str, str]:
        self.set_step("prep", "run")
        p = Path(j.src)
        BROWSE_ROOT.mkdir(parents=True, exist_ok=True)
        root = BROWSE_ROOT / sanitize(p.name)
        n = 2
        while root.exists() and (os.path.ismount(root) or any(root.iterdir())):
            root = BROWSE_ROOT / f"{sanitize(p.name)}_{n}"
            n += 1
        root.mkdir(parents=True, exist_ok=True)
        sess = BrowseSession(name=p.name, source=str(p), root=root, keep=j.keep_cache)
        images: list[tuple[str, Path, str, str]] = []      # (tag, file, kind, fstype)
        try:
            if is_container(p):
                man = load_manifest(p)
                if man.get("encryption"):
                    self.crypto = Crypto(j.passphrase or "")
                lay = Layout.from_json(man["layout"])
                parts = {pp.number: pp for pp in lay.parts}
                sel = [it for it in man["items"] if it["id"] in j.browse_ids]
                self._items_table(sel)
                self.set_step("prep", "done")
                self.set_step("unpack", "run")
                chunk_items = [it for it in sel if it.get("method") == "chunks"]
                sel = [it for it in sel if it.get("method") != "chunks"]
                if chunk_items:
                    specs = [json.dumps([[it["id"], str(p / it["file"]), str(p / it["index_file"])]
                                         for it in chunk_items])]
                    proc, fmp = self.start_chunk_server(specs)
                    sess.procs.append(proc)
                    sess.fuse_mounts.append(str(fmp))
                    for it in chunk_items:
                        part = parts.get(it.get("part"))
                        images.append((it["id"], fmp / f"{it['id']}.img",
                                       "fs" if it.get("mapped") else "disk",
                                       (part.fstype if part else "") or ""))
                        self.item_row(it["id"], {"Copy": "instant (no unpacking)",
                                                 "Device": str(fmp / f"{it['id']}.img")})
                    self.log(f"{len(chunk_items)} item(s) opened directly from the backup "
                              "(only the data you read is decompressed).")
                cdir = Path(j.dst) if j.dst else BROWSE_CACHE
                if sel:
                    try:
                        secure_cache_dir(cdir)
                    except (ValueError, OSError) as e:
                        raise JobFailed(f"Folder for unpacked data: {e}")
                filters_needed = bool(man.get("encryption") or man.get("compression"))
                self.start_phase(sum(max(int(it.get("file_bytes") or 1), 1) for it in sel))
                for it in sel:
                    self.check_stop()
                    cache = cdir / f"{sanitize(p.name)}.{it['id']}.img"
                    stamp = Path(str(cache) + ".ok")
                    w = max(int(it.get("file_bytes") or 1), 1)
                    sess.caches += [cache, stamp]
                    if self._own_regular(cache) and self._own_regular(stamp) \
                            and stamp.read_text().strip() == it["stream_sha256"]:
                        self.w_done += w
                        self.item_row(it["id"], {"Copy": "reused earlier unpack", "Device": str(cache)})
                        self.log(f"{it['id']}: reusing {cache}")
                    else:
                        cache.unlink(missing_ok=True)
                        stamp.unlink(missing_ok=True)
                        pc = it["method"] == "partclone"
                        self.item_row(it["id"], {"Copy": "unpacking", "Device": str(cache)})
                        if pc:
                            dst: Dst = CmdDst([it["tool"], "-r", "-W", "-s", "-", "-o", str(cache),
                                               "-L", str(self.pclog)])
                        else:
                            dst = SparseFileDst(cache, int(it["stream_bytes"]))
                        hs = Hasher()
                        filters = decode_filters(man, self.crypto, it)
                        t = Transfer(FileSrc(p / it["file"]), dst, filters, Hasher(),
                                     hs if filters_needed else None)
                        ok = self.xfer(t, it["id"], w, "copy")
                        if t.cancelled:
                            raise JobFailed("stopped by user")
                        stream = hs if filters_needed else t.hA
                        if not ok:
                            raise JobFailed(f"{it['id']} could not be unpacked.")
                        if stream.hexdigest() != it.get("stream_sha256"):
                            raise JobFailed(f"{it['id']}: data does not match its checksum. "
                                            "The backup is damaged.")
                        # partclone -W sizes the file to whole filesystem blocks; filesystems such
                        # as NTFS need the full partition size (sparse extension, no extra space)
                        want = int(it.get("length") or 0)
                        if want and os.path.getsize(cache) < want:
                            os.truncate(cache, want)
                        stamp.write_text(it["stream_sha256"] + "\n")
                        used = os.stat(cache).st_blocks * 512
                        self.log(f"{it['id']}: unpacked to {cache} ({human(used)} on disk)")
                        self.item_row(it["id"], {"Copy": f"ok, {human(used)} on disk"})
                    part = parts.get(it.get("part"))
                    kind = "fs" if man.get("type") == "fs" else "disk"
                    images.append((it["id"], cache, kind, (part.fstype if part else "") or ""))
                self.set_step("unpack", "done")
            else:
                self.set_step("prep", "done")
                self.set_step("unpack", "skip")
                self.log("Raw image: opened directly, nothing to unpack.")
                images.append(("img", p, "disk", ""))

            # ---- attach + mount ----
            self.set_step("mount", "run")
            for tag, f, kind, fs in images:
                self.check_stop()
                cmd = ["losetup", "-r", "-f", "--show"] + (["-P"] if kind == "disk" else []) + [str(f)]
                r = run(cmd, timeout=60)
                if r.returncode != 0:
                    raise JobFailed(f"losetup failed for {f}: {(r.stderr or r.stdout).strip()}")
                lp = r.stdout.strip()
                sess.loops.append(lp)
                targets: list[tuple[str, str]] = [(tag, lp)]
                if kind == "disk":
                    run(["partx", "-a", lp], timeout=30)
                    if shutil.which("udevadm"):
                        run(["udevadm", "settle", "-t", "10"], timeout=20)
                    kp = kernel_parts(lp)
                    if kp:
                        targets = [(f"p{num}", node) for num, (node, _s, _z) in sorted(kp.items())]
                for ttag, node in targets:
                    info = blkid_probe(node)
                    fstype = info.get("TYPE") or fs
                    label = info.get("LABEL", "")
                    if fstype not in MOUNTABLE:
                        if fstype not in ("", "swap"):
                            self.log(f"[$warning]{ttag}: {fstype} cannot be opened here, skipped.[/]")
                        continue
                    mp = root / sanitize(f"{ttag}_{label}" if label else f"{ttag}_{fstype}")
                    viewed = fstype not in OWNERLESS_FS and view_available()
                    if not viewed:
                        if fstype not in OWNERLESS_FS:
                            self.log(f"[$warning]{ttag}: files keep their owners - private files can "
                                     "only be copied with 'Copy to...' (install python-pyfuse3 and "
                                     "python-trio to make everything readable for you).[/]")
                        err = mount_ro(node, fstype, str(mp))
                        if err:
                            self.log(f"[$error]{ttag} ({fstype}): mount failed: {escape(err)}[/]")
                            continue
                    else:
                        # Linux filesystems keep their owners and permissions: mount them where only
                        # root can look, and show them through a view in which everything is yours
                        stage = stage_dir(f"{root.name}-{ttag}")
                        err = mount_ro(node, fstype, str(stage))
                        if err:
                            self.log(f"[$error]{ttag} ({fstype}): mount failed: {escape(err)}[/]")
                            continue
                        sess.stages.append(str(stage))
                        try:
                            sess.views.append(start_view(stage, mp))
                        except ValueError as e:
                            self.log(f"[$error]{ttag} ({fstype}): {escape(str(e))}[/]")
                            continue
                    sess.mounts.append((str(mp), f"{fstype} {label}".strip()))
                    self.log(f"{ttag}: {fstype} {escape(label)} mounted read-only at {mp}"
                             + (" (all files readable by you)" if viewed else ""))
            if not sess.mounts:
                raise JobFailed("Nothing could be mounted.")
            self.set_step("mount", "done")
        except BaseException:
            sess.keep = sess.keep and not self.stop_requested
            close_browse(sess, force=True)
            raise
        self.browse_session = sess
        return f"READY - {len(sess.mounts)} partition(s) mounted read-only", "$success"

    @staticmethod
    def _own_regular(p: Path) -> bool:
        try:
            st = os.lstat(p)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_uid in trusted_uids()

    # ======================================================================
    #  check
    # ======================================================================
    def _run_check(self, j: Job) -> tuple[str, str]:
        self.set_step("prep", "run")
        self.sink.button("skip", True)
        p = Path(j.src)
        if is_container(p):
            man = load_manifest(p)
            lay = Layout.from_json(man["layout"])
            for line in layout_lines(lay):
                self.log(escape(line))
            self.log(strip_markup(container_summary(man)).replace("\n", "   "))
            items = [i for i in man["items"] if i.get("file")]
            self._items_table(items)
            if not man.get("complete"):
                self.log("[b $error]The backup is marked incomplete; it cannot be restored.[/]")
            self.set_step("prep", "done")
            self.set_step("verify", "run")
            st = self.verify_container_files(p, man, decode=True)
            self.set_step("verify", {"ok": "done", "skipped": "skip"}.get(st, "fail"))
            if st == "ok" and man.get("complete"):
                man["verified"] = datetime.now().isoformat(timespec="seconds")
                try:
                    save_manifest(p, man)
                except OSError:
                    pass
                bad = man.get("bad_bytes") or 0
                if bad:
                    return f"OK - intact ({human(bad)} were unreadable at backup time)", "$warning"
                return "OK - backup is intact", "$success"
            if st == "skipped":
                return "CHECK SKIPPED", "$warning"
            if not man.get("complete"):
                return "INCOMPLETE BACKUP", "$error"
            return "DAMAGED - see log", "$error"
        # legacy raw .img
        self.set_step("prep", "done")
        self.set_step("verify", "run")
        cf = Path(j.src + ".sha256")
        want = cf.read_text().split()[0].lower() if cf.exists() else None
        mp = Path(j.src + ".map")
        bad = image_unreadable_bytes(j.src)
        if mp.exists():
            self.log(f"Mapfile: {human(bad)} unreadable at backup time.")
        self.start_phase(j.size)
        h = Hasher()
        t = Transfer(FileSrc(p), NullDst(), (), h)
        ok = self.xfer(t, p.name, j.size, "verify")
        if t.cancelled:
            self.set_step("verify", "skip")
            return "CHECK SKIPPED", "$warning"
        if not ok:
            self.set_step("verify", "fail")
            return "READ ERROR - see log", "$error"
        self.log(f"SHA-256: {h.hexdigest()}")
        if want is None:
            self.set_step("verify", "warn")
            self.log("[$warning]No .sha256 file next to the image (it is written when a backup is "
                      "verified). The file was readable, but there is nothing to compare against.[/]")
            return "READABLE - no checksum to compare", "$warning"
        if want == h.hexdigest():
            if bad:
                self.set_step("verify", "warn")
                return f"OK - intact ({human(bad)} were unreadable at backup time)", "$warning"
            self.set_step("verify", "done")
            return "OK - image matches its checksum", "$success"
        self.set_step("verify", "fail")
        return "DAMAGED - checksum mismatch", "$error"




# ----------------------------------------------------------------------------
#  Restoring onto a smaller disk ("shrink on restore")
#
#  A filesystem-aware backup of a whole disk can go onto a smaller disk when the data fits:
#  one partition (NTFS, ext2/3/4 or btrfs) is made smaller and the partitions behind it move
#  forward. That partition is restored through a temporary device-mapper device of its original
#  size: the first part is the (smaller) partition on the target, the cut-off rest a sparse
#  temporary file. Then the filesystem is shrunk until it fits, and the temporary device removed.
# ----------------------------------------------------------------------------
SHRINKABLE_FS = ("ntfs", "ext2", "ext3", "ext4", "btrfs")


@dataclass
class ShrinkPlan:
    part: int                                  # number of the partition that is made smaller
    fstype: str
    label: str
    old_size: int
    new_size: int
    shift: int                                 # bytes the partitions behind it move forward
    new_parts: dict[int, list[int]]            # partition number -> [start, size] on the target
    min_size: int                              # estimated minimum size of that filesystem
    tmp_need: int                              # temporary space needed (data in the cut-off part)
    target_size: int


def _item_for_part(items: list[dict], num: int) -> dict | None:
    return next((i for i in items if i.get("role") == "part" and i.get("part") == num), None)


def _used_estimate(it: dict) -> int | None:
    """Bytes of data in a filesystem-aware item (None for raw copies)."""
    if it.get("method") == "partclone" or (it.get("method") == "chunks" and it.get("mapped")):
        return int(it.get("stream_bytes") or 0)
    return None


def _tail_bytes(it: dict, cdir: Path | None, new_size: int, fallback: int) -> int:
    """Bytes of stored data at or beyond new_size inside the partition (exact for chunked backups)."""
    if it.get("method") == "chunks" and cdir is not None and it.get("index_file"):
        try:
            idx = ChunkIndex(cdir / it["index_file"])
            return sum(usz for i in range(idx.count)
                       for (_o, _c, usz, flag, _h) in [idx.entry(i)]
                       if flag in (F_ZSTD, F_PLAIN, F_ZERO) and (i + 1) * idx.chunk > new_size)
        except OSError:
            pass
    return fallback


def shrink_plan(lay: "Layout", items: list[dict], target_size: int, cdir: Path | None = None) -> ShrinkPlan:
    """How a whole-disk, filesystem-aware backup fits onto a smaller disk. Raises ValueError with
    the reason when it cannot."""
    short = lay.needed - target_size
    if lay.kind != "disk":
        raise ValueError("Only backups of whole disks with a partition table can be restored onto a "
                         "smaller disk.")
    if lay.label not in ("gpt", "dos"):
        raise ValueError("Unsupported partition table.")
    if lay.label == "dos" and any(p.method == "skip" or (p.number or 0) >= 5 for p in lay.parts):
        raise ValueError("MBR disks with extended/logical partitions cannot be restored onto a smaller "
                         "disk yet.")
    shift = -(-short // MIB) * MIB                      # round up to whole MiB: keeps alignment
    best = None
    for p in lay.parts:
        it = _item_for_part(items, p.number)
        if it is None or p.fstype not in SHRINKABLE_FS:
            continue
        used = _used_estimate(it)
        if used is None:
            continue
        # estimate only: the filesystem tool checks the real minimum before anything is shrunk
        margin = max(64 * MIB, min(512 * MIB, used // 10))
        min_size = -(-int(used * 1.10 + margin) // MIB) * MIB
        new_size = p.size - shift
        if new_size < min_size:
            continue
        slack = new_size - min_size
        if best is None or slack > best[0]:
            best = (slack, p, it, min_size, new_size)
    if best is None:
        cand = [p for p in lay.parts if p.fstype in SHRINKABLE_FS]
        why = ("no partition has enough free space" if cand else
               "only NTFS, ext2/3/4 and btrfs partitions can be made smaller, and there is none")
        raise ValueError(f"The target is {human(short)} smaller than the original disk, and {why}.")
    _slack, bp, bit, min_size, new_size = best
    new_parts = {}
    for p in lay.parts:
        if p.start > bp.start:
            new_parts[p.number] = [p.start - shift, p.size]
        elif p.number == bp.number:
            new_parts[p.number] = [p.start, new_size]
        else:
            new_parts[p.number] = [p.start, p.size]
    tail = _tail_bytes(bit, cdir, new_size, min(shift, _used_estimate(bit) or shift))
    return ShrinkPlan(part=bp.number, fstype=bp.fstype, label=bp.label, old_size=bp.size, new_size=new_size,
                      shift=shift, new_parts=new_parts, min_size=min_size,
                      tmp_need=tail + 64 * MIB, target_size=target_size)


def shrink_lines(sp: ShrinkPlan) -> list[str]:
    name = f"partition {sp.part} ({sp.fstype}" + (f" '{sp.label}'" if sp.label else "") + ")"
    lines = [f"[b $warning]The target disk is smaller than the original disk.[/] {escape(name)} is made smaller: "
             f"{human(sp.old_size)} -> {human(sp.new_size)} (its data needs about {human(sp.min_size)}).",
             f"Partitions behind it move {human(sp.shift)} forward. Temporary space needed: up to "
             f"{human(sp.tmp_need)} next to the backup."]
    if sp.fstype == "ntfs":
        lines.append("[dim]Windows checks this partition once (chkdsk) at its next start; that is normal "
                     "after resizing.[/]")
    return lines


def adjust_sfdisk_dump(dump: str, new_parts: dict[int, list[int]], ss: int) -> str:
    """sfdisk -d output with new start/size for the given partitions (and without the fields that
    describe the old disk, so that sfdisk fits the table to the target)."""
    out = []
    for line in dump.splitlines():
        if line.startswith(("last-lba:", "device:")):
            continue
        m = re.match(r"^(\S+?)(\d+)\s*:\s*(.*)$", line)
        if m and int(m.group(2)) in new_parts:
            start, size = new_parts[int(m.group(2))]
            fields = re.sub(r"start=\s*\d+", f"start={start // ss}", m.group(3))
            fields = re.sub(r"size=\s*\d+", f"size={size // ss}", fields)
            line = f"{m.group(1)}{m.group(2)} : {fields}"
        out.append(line)
    return "\n".join(out) + "\n"


def shrink_tmp_dir(cdir: Path, need: int) -> Path:
    """Where the cut-off part may be kept during the restore: next to the backup, or diskimg's own
    folder. Never in RAM."""
    cands = [cdir.parent, STATE_DIR / "shrink-tmp"]
    for c in cands:
        base = existing_ancestor(c)
        try:
            free = shutil.disk_usage(base).free
        except OSError:
            continue
        if fstype_of(base) not in NO_CACHE_FS and free >= need + 256 * MIB:
            return c
    raise ValueError(f"Restoring onto the smaller disk needs up to {human(need)} of temporary space on a "
                     f"real disk (next to the backup, {cdir.parent}), but there is not enough.")


def ntfs_min_size(dev: str) -> int | None:
    r = run(["ntfsresize", "-f", "-i", "-P", dev], timeout=600)
    m = re.search(r"You might resize at (\d+) bytes", r.stdout or "")
    return int(m.group(1)) if m else None


def shrink_filesystem(fstype: str, dev: str, new_size: int, log) -> None:
    """Shrink the filesystem on dev to at most new_size bytes. Raises ValueError."""
    if fstype == "ntfs":
        if not shutil.which("ntfsresize"):
            raise ValueError("ntfsresize is missing (pacman -S ntfsprogs).")
        mn = ntfs_min_size(dev)
        if mn is not None and mn > new_size:
            raise ValueError(f"The NTFS filesystem cannot be made smaller than {human(mn)}.")
        r = run(["ntfsresize", "-f", "-P", "-s", str(new_size), dev], timeout=None)
        if r.returncode != 0:
            raise ValueError("ntfsresize failed: " + " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:]))
        log(f"NTFS made smaller to {human(new_size)}.")
    elif fstype in ("ext2", "ext3", "ext4"):
        r = run(["e2fsck", "-f", "-y", dev], timeout=None)
        if r.returncode not in (0, 1):
            raise ValueError("e2fsck failed: " + " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:]))
        r = run(["resize2fs", dev, f"{new_size // 1024}K"], timeout=None)
        if r.returncode != 0:
            raise ValueError("resize2fs failed: " + " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:]))
        log(f"{fstype} made smaller to {human(new_size)}.")
    elif fstype == "btrfs":
        mp = tempfile.mkdtemp(prefix="diskimg-btrfs-", dir="/run")
        try:
            r = run(["mount", "-t", "btrfs", dev, mp], timeout=300)
            if r.returncode != 0:
                raise ValueError("btrfs could not be mounted: " + (r.stderr or r.stdout).strip())
            try:
                r = run(["btrfs", "filesystem", "resize", str(new_size), mp], timeout=None)
                if r.returncode != 0:
                    raise ValueError("btrfs resize failed: " + (r.stderr or r.stdout).strip())
            finally:
                run(["umount", mp], timeout=300)
        finally:
            try:
                os.rmdir(mp)
            except OSError:
                pass
        log(f"btrfs made smaller to {human(new_size)}.")
    else:
        raise ValueError(f"{fstype} cannot be made smaller.")


def check_filesystem(fstype: str, dev: str) -> str | None:
    """Read-only consistency check after shrinking. Returns a problem description or None."""
    if fstype == "ntfs":
        r = run(["ntfsresize", "-f", "-i", "-P", dev], timeout=600)
        ok = r.returncode == 0
    elif fstype in ("ext2", "ext3", "ext4"):
        r = run(["e2fsck", "-f", "-n", dev], timeout=None)
        ok = r.returncode == 0
    elif fstype == "btrfs":
        r = run(["btrfs", "check", "--readonly", dev], timeout=None)
        ok = r.returncode == 0
    else:
        return None
    return None if ok else " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:])


def patch_ntfs_start(dev: str, start_sectors: int) -> None:
    """Moved NTFS partition: update the 'hidden sectors' field (partition start) in both boot sectors."""
    fd = os.open(dev, os.O_RDWR | os.O_CLOEXEC)
    try:
        bs = os.pread(fd, 512, 0)
        if bs[3:11] != b"NTFS    ":
            return
        val = struct.pack("<I", start_sectors & 0xFFFFFFFF)
        pwrite_all(fd, val, 0x1C)
        loc = ntfs_backup_bootsector(dev, os.lseek(fd, 0, os.SEEK_END))
        if loc:
            pwrite_all(fd, val, loc[0] + 0x1C)
        os.fsync(fd)
    finally:
        os.close(fd)


class BatchRunner:
    """Runs several jobs one after the other (several sources backed up at once). Offers the same
    interface as JobRunner, delegating to the job that is currently running."""

    def __init__(self, jobs: list[Job], sink: JobSink | None = None):
        self.sink = sink or JobSink()
        self.runners = [JobRunner(j, self.sink) for j in jobs]
        self.idx = 0
        self.running = False
        self.stop_all = False
        self.quit_after = False
        self.result: tuple[str, str] | None = None
        self.browse_session = None

    @property
    def cur(self) -> JobRunner:
        return self.runners[self.idx]

    @property
    def job(self) -> Job:
        return self.cur.job

    @property
    def steps(self):
        return self.cur.steps

    @property
    def step_names(self):
        return self.cur.step_names

    @property
    def logpath(self):
        return self.cur.logpath

    def info_lines(self) -> list[str]:
        return [f"[b]Backup {self.idx + 1} of {len(self.runners)}[/b]"] + self.cur.info_lines()

    def has_items_table(self) -> bool:
        return self.cur.has_items_table()

    def start_info(self) -> dict:
        return {**self.cur.start_info(), "info": self.info_lines()}

    def stop_text(self) -> str | None:
        t = self.cur.stop_text()
        rest = len(self.runners) - self.idx - 1
        if t and rest:
            t += f" The remaining {rest} backup(s) are not started."
        return t

    def request_stop(self) -> None:
        self.stop_all = True
        self.cur.request_stop()

    def request_quit(self) -> None:
        self.quit_after = True
        self.stop_all = True
        self.cur.request_quit()

    def request_skip(self) -> bool:
        return self.cur.request_skip()

    def run(self) -> tuple[str, str]:
        self.running = True
        results: list[tuple[str, str]] = []
        for i, r in enumerate(self.runners):
            if self.stop_all:
                results.append(("NOT STARTED", "$warning"))
                continue
            self.idx = i
            if i:
                self.sink.begin(self.start_info())
            r.log(f"[b]Backup {i + 1} of {len(self.runners)}: {escape(r.job.src_label)}[/b]")
            results.append(r.run())
        n = len(results)
        ok = sum(1 for _, st in results if st == "$success")
        self.sink.log("[b]Summary[/b]")
        for r, (res, st) in zip(self.runners, results):
            self.sink.log(f"[{st}]{escape(r.job.src)}: {escape(res)}[/]")
        if ok == n:
            self.result = (f"SUCCESS - all {n} backups written and verified", "$success")
        elif any(st == "$error" for _, st in results):
            self.result = (f"{n - ok} of {n} backups FAILED or incomplete - see the summary", "$error")
        else:
            self.result = (f"{ok} of {n} backups fully OK, others with warnings - see the summary", "$warning")
        self.running = False
        return self.result


def make_runner(jobs: list[Job], sink: JobSink | None = None):
    return JobRunner(jobs[0], sink) if len(jobs) == 1 else BatchRunner(jobs, sink)


# ----------------------------------------------------------------------------
#  Mounting drives (udisks, so that file managers see the same mounts)
# ----------------------------------------------------------------------------
def mounted_nodes(dev: Dev) -> list[str]:
    """Device nodes in dev's subtree that are mounted, deepest first."""
    nodes = [x for x in dev.subtree() if x.mounts]
    return [x.path for x in sorted(nodes, key=lambda x: -x.depth)]


def mount_cmd(node: str, readonly: bool) -> list[str]:
    return ["udisksctl", "mount", "-b", node] + (["-o", "ro"] if readonly else [])


def unmount_cmd(node: str) -> list[str]:
    return ["udisksctl", "unmount", "-b", node]


def device_properties(d: Dev) -> list[tuple[str, str]]:
    rows = [("Device", d.path), ("Size", f"{human(d.size)} ({d.size:,} bytes)"), ("Type", d.kind),
            ("Label", d.label or "-"), ("Filesystem", d.fstype or "-"), ("UUID", d.uuid or "-")]
    disk = d if d.kind == "disk" else next((a for a in d.ancestors() if a.kind == "disk"), None)
    if disk is not None:
        rows += [("Disk", disk.path), ("Model", disk.model or "-"), ("Serial number", disk.serial or "-"),
                 ("Partition table", (disk.pttype or "-").upper()), ("Connection", disk.tran or "-")]
    mounts = [m for x in d.subtree() for m in x.mounts]
    rows += [("Mounted at", ", ".join(mounts) or "not mounted"), ("Status", d.busy or ("read-only" if d.ro else "ok"))]
    return rows


# ----------------------------------------------------------------------------
#  Planning (shared by the text UI and the GTK helper)
# ----------------------------------------------------------------------------
@dataclass
class PlanInput:
    """Everything the user chose for a disk job, independent of the user interface."""
    mode: str = "d2i"
    method: str = "raw"            # raw | fs (d2i, d2d)
    compression: int = 0
    encrypt: bool = False
    verify: bool = True
    precheck: bool = True
    fallback_raw: bool = True
    allow_btrfs: bool = False
    sparse: bool = True
    zero_fill: bool = True
    fix_gpt: bool = True
    idirect: bool = False
    retries: int = 1
    keep_cache: bool = False
    src_dev: str = ""              # device path (d2i, d2d)
    src_devs: list[str] = field(default_factory=list)   # several sources (d2i): one backup each
    dst_dev: str = ""              # device path (d2d, i2d)
    src_path: str = ""             # backup file/folder (i2d, chk, brw)
    dst_path: str = ""             # backup to create (d2i)
    browse_ids: list[str] = field(default_factory=list)
    cache_path: str = ""

    @staticmethod
    def from_dict(d: dict) -> "PlanInput":
        keys = PlanInput.__dataclass_fields__
        pi = PlanInput(**{k: v for k, v in d.items() if k in keys})
        pi.compression = int(pi.compression or 0)
        pi.retries = int(pi.retries or 0)
        pi.browse_ids = [str(x) for x in (pi.browse_ids or [])]
        pi.src_devs = [str(x) for x in (pi.src_devs or []) if x]
        if len(pi.src_devs) == 1 and not pi.src_dev:
            pi.src_dev = pi.src_devs[0]
        return pi


_man_lock = threading.Lock()
_man_cache: dict[str, tuple[float, dict | None]] = {}


def read_manifest(p: Path) -> dict | None:
    """Manifest of a .dimg folder (cached by modification time), or None."""
    try:
        mt = (p / MANIFEST).stat().st_mtime
    except OSError:
        return None
    key = str(p)
    with _man_lock:
        c = _man_cache.get(key)
        if c and c[0] == mt:
            return c[1]
    try:
        man = load_manifest(p)
    except Exception:  # noqa: BLE001
        man = None
    with _man_lock:
        _man_cache[key] = (mt, man)
    return man


def devs_overlap(a: Dev, b: Dev) -> bool:
    return a.path == b.path or a in list(b.ancestors()) or b in list(a.ancestors())


def plan_engine(pi: PlanInput) -> str:
    m = pi.mode
    if m == "brw":
        return "browse"
    if m == "chk":
        return "check"
    if m == "i2d":
        raw = pi.src_path.strip()
        return "container" if raw and is_container(Path(raw)) else "ddrescue"
    if pi.method == "fs":
        return "container"
    if m == "d2i" and (pi.compression or pi.encrypt):
        return "container"
    return "ddrescue"


def wanted_ext(pi: PlanInput) -> str:
    return CONTAINER_EXT if plan_engine(pi) == "container" else ".img"


def default_backup_name(dev: Dev | None, ext: str) -> str:
    base = sanitize(dev.model or dev.label or dev.name) if dev else "disk"
    if dev and dev.serial:
        base += "_" + sanitize(dev.serial)
    if dev and dev.kind == "part" and not base.startswith(sanitize(dev.name)):
        base += "_" + dev.name
    return f"{base}_{datetime.now():%Y%m%d-%H%M}{ext}"


def browse_rows_for(path: str) -> list[dict]:
    """Partitions of a backup that can be opened (see browse_candidates)."""
    raw = path.strip()
    if not raw:
        return []
    p = Path(raw)
    try:
        if is_container(p):
            man = read_manifest(p)
            return browse_candidates(p, man) if man else []
        if p.is_file():
            return browse_candidates(p, None)
    except Exception:  # noqa: BLE001
        return []
    return []


def default_browse_selection(rows: list[dict]) -> list[str]:
    ok_rows = [r for r in rows if r["ok"]]
    return [max(ok_rows, key=lambda r: r["size"])["id"]] if ok_rows else []


def plan_job(pi: PlanInput, devs: dict[str, Dev]) -> Job:
    """Validate the choices and build a Job. Raises ValueError with a message for the user."""
    m = pi.mode
    if m not in MODES or m == "fbk":
        raise ValueError("Unknown mode.")
    eng = plan_engine(pi)
    method = pi.method if m in ("d2i", "d2d") else "raw"
    src_dev = dst_dev = None
    man = None
    mapfile = None
    if m in ("d2i", "d2d"):
        if not pi.src_dev or pi.src_dev not in devs:
            raise ValueError("Select a source device.")
        src_dev = devs[pi.src_dev]
        if src_dev.busy:
            raise ValueError(f"Source {src_dev.path} is {src_dev.busy}.")
        src, size, src_label = src_dev.path, src_dev.size, src_dev.long
    else:
        raw = pi.src_path.strip()
        if not raw:
            raise ValueError("Choose the backup (.img file or .dimg folder).")
        p = Path(raw).expanduser()
        if is_container(p):
            man = read_manifest(p)
            if man is None:
                raise ValueError(f"{p}/{MANIFEST} is unreadable or from an unknown format.")
            if not man.get("complete") and m == "i2d":
                raise ValueError("This backup is incomplete (the backup job did not finish).")
            size = int(man["layout"]["needed"])
            src, src_label = str(p.resolve()), f"{p.resolve()}"
            method = man.get("type", "raw")
        elif p.is_file():
            size = p.stat().st_size
            if size == 0:
                raise ValueError(f"{p} is empty.")
            src, src_label = str(p.resolve()), f"{p.resolve()}  {human(size)}"
        elif p.is_dir():
            raise ValueError(f"{p} is a folder but not a diskimg backup (no {MANIFEST}).")
        else:
            raise ValueError(f"{p} does not exist.")

    if m == "chk":
        return Job(mode=m, engine="check", method=method, src=src, dst="", size=size,
                   src_label=src_label, dst_label="", src_dev=None, dst_dev=None, manifest=man)

    if m == "brw":
        if man is not None and not man.get("complete"):
            raise ValueError("This backup is incomplete and cannot be opened.")
        rows = {r["id"]: r for r in browse_rows_for(src)}
        sel = [i for i in pi.browse_ids if i in rows and rows[i]["ok"]]
        if not sel:
            raise ValueError("Select at least one partition to open.")
        need = sum(rows[i]["need"] for i in sel)
        cache = pi.cache_path.strip()
        if need:
            if not cache.startswith("/"):
                raise ValueError("Enter an absolute folder for the unpacked data.")
            base = existing_ancestor(Path(cache))
            fsb = fstype_of(base)
            if fsb in NO_CACHE_FS:
                raise ValueError(f"{base} is on {fsb}, which cannot hold the unpacked data "
                                 "(RAM or no sparse files). Choose a folder on an ext4, btrfs, "
                                 "xfs or NTFS disk.")
            free = shutil.disk_usage(base).free
            exact = not any(rows[i]["max"] for i in sel)
            if exact and free < need + 64 * MIB:
                raise ValueError(f"Not enough space in {base}: needs about {human(need)}, "
                                 f"{human(free)} free.")
        return Job(mode=m, engine="browse", method=method, src=src, dst=cache, size=need,
                   src_label=src_label, dst_label=cache or "-", src_dev=None, dst_dev=None,
                   manifest=man, browse_ids=sorted(sel), keep_cache=pi.keep_cache)

    shrink = None
    if m in ("d2d", "i2d"):
        if not pi.dst_dev or pi.dst_dev not in devs:
            raise ValueError("Select a target device.")
        dst_dev = devs[pi.dst_dev]
        if dst_dev.busy:
            raise ValueError(f"Target {dst_dev.path} is {dst_dev.busy}.")
        if dst_dev.ro:
            raise ValueError(f"Target {dst_dev.path} is read-only.")
        if src_dev and devs_overlap(src_dev, dst_dev):
            raise ValueError("Source and target overlap.")
        if man is not None:
            lay = man["layout"]
            if lay["kind"] == "disk" and dst_dev.kind == "part":
                raise ValueError("This backup contains a whole disk with a partition table. "
                                 "Select a whole disk as target.")
        shrink = None
        if man is not None and dst_dev.size < size:
            if method != "fs":
                raise ValueError(f"Target too small: this is a raw (sector-by-sector) backup of "
                                 f"{human(size)}; the target has {human(dst_dev.size)}. Only "
                                 "filesystem-aware backups can be restored onto a smaller disk.")
            try:
                shrink = shrink_plan(Layout.from_json(man["layout"]), man.get("items", []), dst_dev.size, p)
            except ValueError as e:
                raise ValueError(f"Target too small: needs {human(size)}, has {human(dst_dev.size)}. {e}")
        elif method == "raw" and dst_dev.size < size:
            raise ValueError(f"Target too small: needs {human(size)} ({size:,} B), "
                             f"has {human(dst_dev.size)} ({dst_dev.size:,} B).")
        dst, dst_label = dst_dev.path, dst_dev.long
        if eng == "ddrescue":
            if m == "d2d":
                mapfile = MAP_DIR / f"clone__{src_dev.ident}__{dst_dev.ident}.map"
            else:
                mapfile = MAP_DIR / f"restore__{sanitize(Path(src).name)}_{size}__{dst_dev.ident}.map"
    else:
        raw = pi.dst_path.strip()
        if not raw or raw.endswith("/"):
            raise ValueError("Enter a name for the backup.")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise ValueError("Use an absolute path for the backup.")
        if not p.parent.is_dir():
            raise ValueError(f"Folder {p.parent} does not exist.")
        ext = wanted_ext(pi)
        if not p.name.endswith(ext):
            raise ValueError(f"This backup type is saved as '{ext}'. Change the name to end in {ext}.")
        for suffix in (".partial", ".old"):
            if p.name.endswith(ext + suffix):
                raise ValueError(f"Names ending in {ext}{suffix} are used by diskimg itself.")
        rp = p.parent.resolve() / p.name
        if str(rp).startswith(("/dev/", "/proc/", "/sys/")):
            raise ValueError("The backup must go to a mounted filesystem.")
        if src_dev:
            mp = {m_ for x in src_dev.subtree() for m_ in x.mounts}
            if any(str(rp).startswith(x.rstrip("/") + "/") for x in mp):
                raise ValueError("The backup cannot be stored on the source disk.")
        if eng == "ddrescue":
            if rp.exists() and not rp.is_file():
                raise ValueError(f"{rp} exists and is not a regular file.")
        elif rp.exists():
            if not rp.is_dir():
                raise ValueError(f"{rp} exists and is not a folder.")
            if not is_container(rp) and any(rp.iterdir()):
                raise ValueError(f"{rp} is a non-empty folder that is not a diskimg backup.")
        fs = fstype_of(rp.parent)
        if fs in ("vfat", "msdos", "fat", "fat32") and size > FAT_LIMIT:
            raise ValueError(f"Target filesystem is {fs}: files are limited to 4 GiB.")
        if fs in ("tmpfs", "overlay", "ramfs", "squashfs"):
            raise ValueError(f"{rp.parent} is on {fs} (RAM/ISO). Mount a real disk first.")
        free = shutil.disk_usage(rp.parent).free
        if eng == "ddrescue":
            existing = rp.stat().st_size if rp.is_file() else 0
            if free + existing < size and not pi.sparse:
                raise ValueError(f"Not enough free space: needs {human(size)}, "
                                 f"{human(free + existing)} available.")
            mapfile = Path(str(rp) + ".map")
        elif method == "raw" and not pi.compression and free < size:
            raise ValueError(f"Not enough free space: needs {human(size)}, {human(free)} available.")
        net = net_share_of(rp.parent)
        dst, dst_label = str(rp), (f"{rp}  (NAS: network share {net}, {human(free)} free)" if net
                                   else f"{rp}  (on {fs}, {human(free)} free)")

    return Job(
        shrink=shrink if m == "i2d" else None,
        mode=m, engine=eng, method=method, src=src, dst=dst, size=size,
        src_label=src_label, dst_label=dst_label, src_dev=src_dev, dst_dev=dst_dev,
        mapfile=mapfile,
        verify=bool(pi.verify),
        sparse=bool(pi.sparse) and m == "d2i",
        zero_fill=bool(pi.zero_fill),
        fix_gpt=bool(pi.fix_gpt) and dst_dev is not None,
        idirect=bool(pi.idirect) and src_dev is not None and eng == "ddrescue",
        retries=int(pi.retries or 0),
        compression=int(pi.compression or 0) if m == "d2i" else 0,
        encrypt=bool(pi.encrypt) and m == "d2i",
        allow_btrfs=bool(pi.allow_btrfs),
        fallback_raw=bool(pi.fallback_raw),
        precheck=bool(pi.precheck),
        manifest=man,
    )


def is_batch(pi: PlanInput) -> bool:
    return pi.mode == "d2i" and len(pi.src_devs) > 1


def plan_jobs(pi: PlanInput, devs: dict[str, Dev]) -> list[Job]:
    """Like plan_job, but with several sources (Disk -> Image) one backup per source: the
    destination is then a folder and every backup gets its default name in it."""
    if not is_batch(pi):
        return [plan_job(pi, devs)]
    srcs = pi.src_devs
    for a in srcs:
        if a not in devs:
            raise ValueError(f"{a} is no longer connected.")
    for i, a in enumerate(srcs):
        for b in srcs[i + 1:]:
            if devs_overlap(devs[a], devs[b]):
                raise ValueError(f"{a} and {b} overlap (a disk and one of its partitions). Select only one of them.")
    folder = Path(pi.dst_path.strip() or "/").expanduser()
    for ext in (".img", CONTAINER_EXT):
        if folder.name.endswith(ext) and not folder.is_dir():
            folder = folder.parent
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError(f"Choose an existing folder for the {len(srcs)} backups.")
    jobs, names = [], set()
    for d in srcs:
        name = default_backup_name(devs[d], wanted_ext(pi))
        while name in names:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}_2.{ext}"
        names.add(name)
        sub = PlanInput(**{**asdict(pi), "src_dev": d, "src_devs": [], "dst_path": str(folder / name)})
        try:
            jobs.append(plan_job(sub, devs))
        except ValueError as e:
            raise ValueError(f"{d}: {e}")
    return jobs


def batch_summary(jobs: list[Job]) -> str:
    if len(jobs) == 1:
        return plan_summary(jobs[0])
    folder = Path(jobs[0].dst).parent
    names = ", ".join(Path(j.src).name for j in jobs)
    return (f"[b $success]Ready[/]  {len(jobs)} backups ({escape(names)}) [dim]->[/] {escape(str(folder))}  "
            f"[dim]one after the other[/]")


def plan_summary(j: Job) -> str:
    """One line (markup) describing a valid job."""
    if j.mode == "chk":
        return f"[b $success]Ready[/]  check {escape(Path(j.src).name)}"
    if j.mode == "brw":
        return (f"[b $success]Ready[/]  open {', '.join(j.browse_ids)} of {escape(Path(j.src).name)}"
                + (f"  [dim]unpack ~{human(j.size)}[/]" if j.size else ""))
    extra = ""
    if j.dst_is_dev and j.method == "raw" and j.dst_size > j.size:
        extra = f"  [dim](target {human(j.dst_size - j.size)} larger)[/]"
    what = human(j.size) if j.method == "raw" else "used data"
    if j.shrink:
        extra = f"  [$warning](smaller disk: partition {j.shrink.part} is made smaller)[/]"
    return (f"[b $success]Ready[/]  {escape(Path(j.src).name)} [dim]->[/] {escape(j.dst)}  "
            f"[b]{what}[/]{extra}")


def prepare_job(job: Job, used_bytes=None) -> list[str]:
    """Analyse the source / backup before the confirmation (reads partition tables).
    used_bytes: optional function node -> bytes in use, for the layout display.
    Returns warnings; raises ValueError when the job cannot run."""
    warnings: list[str] = []
    if job.engine == "container" and job.mode in ("d2i", "d2d"):
        try:
            job.layout = analyze(job.src_dev, job.method, job.allow_btrfs)
        except OSError as e:
            raise ValueError(str(e))
        if used_bytes is not None:
            for p in job.layout.parts:
                v = used_bytes(p.node)
                if v:
                    p.used = v
        if job.method == "fs":
            job.size = sum(p.size for p in job.layout.parts if p.method != "skip")
            if not any(p.method == "partclone" for p in job.layout.parts):
                warnings.append("No filesystem on the source can be copied filesystem-aware; "
                                "everything would be copied raw.")
        if job.mode == "d2d":
            if job.layout.kind == "disk" and job.dst_dev.kind == "part":
                raise ValueError("The source is a whole disk with a partition table. Select a whole "
                                 "disk as target.")
            if job.dst_dev.size < job.layout.needed:
                raise ValueError(f"Target too small: needs {human(job.layout.needed)}, has "
                                 f"{human(job.dst_dev.size)}.")
            tss = sector_size(job.dst)
            if job.layout.kind == "disk" and tss != job.layout.sector_size:
                raise ValueError(f"Sector sizes differ (source {job.layout.sector_size}, target "
                                 f"{tss}). A partition table cannot be copied.")
    if job.manifest is not None:
        job.layout = Layout.from_json(job.manifest["layout"])
        if job.mode == "i2d" and job.layout.kind == "disk":
            tss = sector_size(job.dst)
            if tss != job.layout.sector_size:
                raise ValueError(f"Sector sizes differ (image {job.layout.sector_size}, target {tss}).")
    return warnings


def dst_exists(job: Job) -> bool:
    return bool(job.dst) and not job.dst_is_dev and Path(job.dst).exists()


def resume_question(job: Job) -> str | None:
    """Text of the 'resume previous job?' question for ddrescue jobs, or None."""
    if not (job.engine == "ddrescue" and job.mapfile and job.mapfile.exists()
            and (job.dst_is_dev or dst_exists(job))):
        return None
    try:
        done = map_totals(parse_mapfile(job.mapfile)).get("+", 0)
    except Exception:  # noqa: BLE001
        done = 0
    body = (f"An earlier job with the same source and target was found.\n"
            f"Mapfile: {job.mapfile}\nAlready copied: {human(done)} of {human(job.size)}\n\n"
            "Resume continues where it stopped. Start over discards the progress.")
    if job.dst_is_dev:
        body += "\n\nOnly resume if nothing has written to the target since the earlier run."
    return body


def apply_resume_choice(job: Job, choice: str | None) -> None:
    """choice: 'resume', 'fresh', or None when no question was asked."""
    exists = dst_exists(job)
    if choice is None:
        job.resume = False
        job.replace_existing = exists
    else:
        job.resume = choice == "resume"
        job.replace_existing = choice == "fresh" and exists


def passphrase_kind(job: Job) -> str | None:
    """'new' (encrypted backup), 'existing' (encrypted container), or None."""
    if job.encrypt:
        return "new"
    if job.manifest is not None and job.manifest.get("encryption"):
        return "existing"
    return None


def check_passphrase(job: Job, pw: str) -> bool:
    return test_passphrase(Path(job.src), job.manifest, pw)


def confirm_token(job: Job) -> str:
    """What the user must type before a device is overwritten ('' = no typing needed)."""
    return Path(job.dst).name if job.dst_is_dev else ""


def confirm_lines_for(jobs: list[Job]) -> list[str]:
    if len(jobs) == 1:
        return confirm_lines(jobs[0])
    j = jobs[0]
    lines = [f"[b]{len(jobs)} backups, one after the other[/b]",
             f"[b]Engine[/b]   {escape(engine_label(j.mode, j.method, j.compression, j.encrypt, False))}"]
    if j.engine == "container":
        lines.append(f"[b]Options[/b]  compression: {'zstd -' + str(j.compression) if j.compression else 'off'}"
                     f"   encryption: {'AES-256 (gpg)' if j.encrypt else 'off'}")
    lines.append("")
    for n, x in enumerate(jobs, 1):
        lines.append(f"{n}. {escape(x.src_label)}")
        lines.append(f"   [dim]->[/] {escape(x.dst)}")
        if x.layout and x.method == "fs":
            for w in x.layout.warnings:
                lines.append(f"   [$warning]! {escape(w)}[/]")
    lines += ["", "[dim]If one backup fails, the others still run. The result lists each one.[/]"]
    return lines


def confirm_lines(job: Job) -> list[str]:
    """Markup lines of the confirmation dialog."""
    j = job
    lines = [
        f"[b]Mode[/b]     {MODES[j.mode][0]}",
        f"[b]Engine[/b]   {escape(engine_label(j.mode, j.method, j.compression, j.encrypt, j.manifest is not None))}",
        f"[b]Source[/b]   {escape(j.src_label)}",
    ]
    if j.mode != "chk":
        lines.append(f"[b]Target[/b]   {escape(j.dst_label)}")
    lines.append(f"[b]Size[/b]     {human(j.size)}  ({j.size:,} bytes)")
    if j.engine == "ddrescue":
        lines.append(f"[b]Mapfile[/b]  {escape(str(j.mapfile))}")
        lines.append(f"[b]Run[/b]      {'RESUME previous job' if j.resume else 'new job'}")
    if j.mode == "d2i" and j.engine == "container":
        lines.append(f"[b]Options[/b]  compression: {'zstd -' + str(j.compression) if j.compression else 'off'}"
                     f"   encryption: {'AES-256 (gpg)' if j.encrypt else 'off'}")
    lay = j.layout
    if lay and j.method == "fs":
        lines.append("")
        lines += [escape(x) for x in layout_lines(lay)]
        for w in lay.warnings:
            lines.append(f"[$warning]! {escape(w)}[/]")
        lines.append("[dim]Free space inside filesystems and unpartitioned space are not copied.[/]")
    if j.shrink:
        lines.append("")
        lines += shrink_lines(j.shrink)
    if j.mode in ("d2d", "i2d") and j.dst_dev and j.dst_dev.kind != "part":
        lines.append("[dim]The copy has the same disk/partition IDs as the source. Do not run "
                     "Windows with both disks attached.[/]")
    lines.append("")
    if j.dst_is_dev:
        lines.append(f"[b $error]ALL DATA ON {escape(j.dst)} WILL BE OVERWRITTEN.[/]")
        lines.append(f"Type [b]{escape(confirm_token(j))}[/b] to confirm:")
    elif j.replace_existing:
        lines.append(f"[b $warning]The existing {escape(j.dst)} will be replaced.[/] [dim]It is kept "
                     "until the new backup is complete and verified (needs space for both).[/]")
    return lines


# ----------------------------------------------------------------------------
#  File backup sets: steps before a job (shared by both user interfaces)
# ----------------------------------------------------------------------------
def fileset_job(s: dict, action: str, read_all: bool = False) -> Job:
    return Job(mode="fbk", engine="files" if action == "run" else "files-check", method="files",
               src=", ".join(s["linux"] + [f"Windows:{p}" for p in s["win_paths"]]), dst=s["repo"],
               size=0, src_label=", ".join(s["linux"] + [f"{s.get('win_label') or s['win_uuid']}:{p}"
                                                        for p in s["win_paths"]]),
               dst_label=s["repo"], src_dev=None, dst_dev=None, fileset=s, verify=read_all)


def fileset_precheck(s: dict, action: str) -> dict:
    """What has to happen before run/check/browse of a set. Returns one of
    {"error": msg} | {"new_password": pw, "pwfile": path} | {"need_password": True} | {}."""
    if not shutil.which("restic"):
        return {"error": "restic is not installed (pacman -S restic)."}
    pwf = restic_pwfile(s)
    exists = repo_exists(s["repo"])
    if action != "run" and not exists:
        return {"error": "There is no repository yet. Run a backup first."}
    if not exists:
        if s.get("repo_initialized"):
            return {"error": f"The repository {s['repo']} was not found. Is the NAS / disk connected? "
                             "(diskimg does not create a new, empty repository in its place.)"}
        prob = repo_location_problem(s["repo"])
        if prob:
            return {"error": prob}
        if not pwf.exists():
            write_secret(pwf, secrets.token_urlsafe(24))
        return {"new_password": pwf.read_text().strip(), "pwfile": str(pwf)}
    if not pwf.exists():
        return {"need_password": True}
    return {}


def fileset_set_password(s: dict, pw: str) -> bool:
    """Check pw against the existing repository and store it. Returns False if wrong."""
    if not restic_password_ok(s["repo"], pw):
        return False
    write_secret(restic_pwfile(s), pw)
    return True


def fileset_info(s: dict) -> str:
    """One line (markup) about the repository of a set."""
    if not repo_exists(s.get("repo", "")):
        return "[dim]No repository yet. It is created on the first backup.[/]"
    if not restic_pwfile(s).exists():
        return "[$warning]Repository exists, password unknown here. You will be asked for it.[/]"
    snaps = restic_snapshots(s)
    if snaps is None:
        return "[$warning]Repository could not be read (see Check).[/]"
    if not snaps:
        return "No versions yet."
    last = max(x.get("time", "") for x in snaps)[:16].replace("T", " ")
    return f"[b]{len(snaps)}[/b] version(s), latest {last}"


def validate_fileset(d: dict, old: dict | None, ntfs: list[tuple[str, str]]) -> dict:
    """Check a set entered by the user and return the stored form. Raises ValueError."""
    old = old or {}
    name = str(d.get("name", "")).strip()
    repo = str(d.get("repo", "")).strip()
    linux = [x.strip() for x in d.get("linux", []) if str(x).strip()]
    winp = [x.strip() for x in d.get("win_paths", []) if str(x).strip()]
    win = str(d.get("win_uuid") or "")
    if not name:
        raise ValueError("Enter a name for the set.")
    if not repo.startswith("/"):
        raise ValueError("Choose an absolute repository folder.")
    if not linux and not (win and winp):
        raise ValueError("Add at least one Linux folder, or a Windows partition with folders.")
    for p in linux:
        if not p.startswith("/") or not os.path.exists(p):
            raise ValueError(f"Linux folder not found: {p}")
    if winp and not win:
        raise ValueError("Select the Windows partition for the Windows folders.")
    rp = Path(repo)
    for p in linux:
        if str(rp).startswith(p.rstrip("/") + "/") or str(rp) == p.rstrip("/"):
            raise ValueError("The repository must not be inside a folder that is backed up.")
    keep = str(d.get("keep") or "")
    if keep not in KEEP_POLICIES:
        keep = ""
    return {"id": old.get("id") or f"set-{int(time.time())}-{secrets.token_hex(3)}",
            "name": name, "repo": repo, "linux": linux, "win_uuid": win,
            "win_label": next((lbl for lbl, v in ntfs if v == win), old.get("win_label", "")),
            "win_paths": ["/" + p.lstrip("/") for p in winp],
            "excludes": [x.strip() for x in d.get("excludes", []) if str(x).strip()],
            "keep": keep,
            "created": old.get("created") or datetime.now().isoformat(timespec="seconds"),
            "repo_initialized": bool(old.get("repo_initialized")) and old.get("repo") == repo}


def ntfs_partitions(devs: dict[str, Dev] | None = None) -> list[tuple[str, str]]:
    """(label, uuid) of NTFS partitions, for the Windows source of file backups."""
    if devs is None:
        try:
            devs = load_devices()
        except Exception:  # noqa: BLE001
            devs = {}
    out = []
    for d in devs.values():
        fstype, uuid, label = d.fstype, d.uuid, d.label
        if not fstype and d.kind in ("part", "loop") and not d.children and not d.busy:
            info = blkid_probe(d.path)
            fstype, uuid, label = info.get("TYPE", ""), info.get("UUID", ""), info.get("LABEL", "")
        if fstype == "ntfs" and uuid:
            out.append((f"{d.path}  {label or '-'}  {human(d.size)}", uuid))
    return out


def start_restic_mount(s: dict) -> tuple[BrowseSession | None, str | None]:
    """Mount all versions of a file backup set read-only. Returns (session, error)."""
    mp = FILES_MNT / sanitize(s["name"])
    if os.path.ismount(mp):
        run(["umount", str(mp)], timeout=30)
    # restic shows the files with their original owners: it mounts where only root can look, and
    # the desktop user sees everything (readable) through a file view
    if not view_available():
        return None, "Opening versions needs FUSE support: install python-pyfuse3 and python-trio."
    stage = stage_dir(f"restic-{s['name']}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logp = LOG_DIR / f"restic-mount-{sanitize(s['name'])}.log"
    with open(logp, "a") as log:
        p = subprocess.Popen(restic_base(s) + ["mount", str(stage)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    _reg(p)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 90:
        if (stage / "snapshots").is_dir():
            sess = BrowseSession(name=f"{s['name']} (file backup versions)", source=s["repo"], root=mp,
                                 procs=[p], fuse_mounts=[str(stage)],
                                 note="Versions are under snapshots/<date> (latest = newest). "
                                      "Read-only.")
            try:
                sess.views.append(start_view(stage, mp))
            except ValueError as e:
                close_browse(sess, force=True)
                return None, f"Could not open the versions: {e}"
            sess.mounts.append((str(mp), "file backup versions"))
            return sess, None
        if p.poll() is not None:
            break
        time.sleep(0.3)
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    _unreg(p)
    run(["umount", "-l", str(stage)], timeout=30)
    try:
        stage.rmdir()
    except OSError:
        pass
    tail = ""
    try:
        tail = logp.read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        pass
    return None, f"Could not open the versions: {tail}"


# ----------------------------------------------------------------------------
#  Process-wide cleanup (both user interfaces)
# ----------------------------------------------------------------------------
def cleanup_procs(exclude: set | None = None) -> None:
    with _proc_lock:
        procs = [p for p in _running_procs if not exclude or p not in exclude]
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
    for p in procs:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()


_shutdown_lock = threading.Lock()
_shutdown_done = False


def shutdown_all(browse: BrowseSession | None, net_mounted: set[str], report=print) -> None:
    """Stop child processes and undo mounts. Safe to call twice (normal exit and signal path);
    a second caller waits until the first has finished. Order matters: job processes first, then
    the browse session top-down (filesystems, loop devices, FUSE mounts, and only then the FUSE
    servers below them), then network shares."""
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True
        cancel_usage()
        cleanup_procs(exclude=set(browse.procs) | set(browse.views) if browse is not None else None)
        if browse is not None:
            for e in close_browse(browse, force=True):
                report(f"diskimg: could not unmount {e}")
        cleanup_procs()
        for mp in sorted(net_mounted):
            e = umount_path(mp, lazy=True)
            if e:
                report(f"diskimg: network share {mp} is still mounted ({e}). "
                       f"Unmount it later with: umount {mp}")


def set_c_locale() -> None:
    """Child programs are parsed by their (English) output: never let a localized message
    (e.g. German 'nicht eingehängt' from umount) change what diskimg decides."""
    os.environ["LC_ALL"] = "C.UTF-8"
    os.environ.pop("LANGUAGE", None)
