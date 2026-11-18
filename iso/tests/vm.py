#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-only
"""Drive the rescue ISO in a headless QEMU VM (for testing).

  vm.py start ISO [--bios] [--disk IMG ...] [--mem MB]   boot the ISO (UEFI unless --bios)
  vm.py shot FILE.png                                    screenshot
  vm.py key COMBO ...                                    e.g. ret, tab, ctrl-alt-f2, alt-f4
  vm.py type TEXT [--layout de]                          type text (guest keyboard layout)
  vm.py click X Y                                        left click (screen pixels of the last shot)
  vm.py serial                                           print the serial console log
  vm.py stop
State (QMP socket, logs) lives in $VM_DIR (default $XDG_RUNTIME_DIR/diskimg-vm).
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

DIR = os.environ.get("VM_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "diskimg-vm")
SOCK = os.path.join(DIR, "qmp.sock")
OVMF_CODE = "/usr/share/edk2/x64/OVMF_CODE.4m.fd"
OVMF_VARS = "/usr/share/edk2/x64/OVMF_VARS.4m.fd"


def qmp(*cmds):
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    f = s.makefile("rwb")
    f.readline()

    def call(c):
        f.write((json.dumps(c) + "\n").encode())
        f.flush()
        while True:
            m = json.loads(f.readline())
            if "return" in m or "error" in m:
                return m
    call({"execute": "qmp_capabilities"})
    res = [call(c) for c in cmds]
    s.close()
    return res


def start(argv):
    iso, bios, disks, mem = None, False, [], 3072
    it = iter(argv)
    for a in it:
        if a == "--bios":
            bios = True
        elif a == "--disk":
            disks.append(next(it))
        elif a == "--mem":
            mem = int(next(it))
        else:
            iso = a
    if not iso:
        sys.exit("vm.py start ISO [--bios] [--disk IMG] [--mem MB]")
    os.makedirs(DIR, exist_ok=True)
    cmd = ["qemu-system-x86_64", "-machine", "q35", "-smp", "2", "-m", str(mem),
           "-cdrom", iso, "-boot", "d", "-display", "none", "-vga", "std",
           "-device", "qemu-xhci", "-device", "usb-tablet",
           "-nic", "user,model=virtio-net-pci",
           "-qmp", f"unix:{SOCK},server=on,wait=off", "-serial", f"file:{DIR}/serial.log",
           "-daemonize", "-pidfile", f"{DIR}/pid"]
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        cmd[1:1] = ["-enable-kvm", "-cpu", "host"]
    if not bios:
        shutil.copy(OVMF_VARS, f"{DIR}/vars.fd")
        cmd += ["-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF_CODE}",
                "-drive", f"if=pflash,format=raw,unit=1,file={DIR}/vars.fd"]
    for d in disks:
        cmd += ["-drive", f"file={d},format=raw,if=virtio"]
    subprocess.run(cmd, check=True)
    print("started", "BIOS" if bios else "UEFI")


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def shot(path):
    path = os.path.abspath(path)
    r = qmp({"execute": "screendump", "arguments": {"filename": path, "format": "png"}})[0]
    if "error" in r:
        sys.exit(r["error"])
    w, h = png_size(path)
    with open(os.path.join(DIR, "size"), "w") as f:
        f.write(f"{w} {h}")
    print(path, w, h)


def keys(combos):
    for c in combos:
        ks = [{"type": "qcode", "data": k} for k in c.split("-")]
        qmp({"execute": "send-key", "arguments": {"keys": ks, "hold-time": 60}})
        time.sleep(0.15)


DE_SWAP = {"y": "z", "z": "y"}
SYMBOLS = {" ": ["spc"], ".": ["dot"], ",": ["comma"], "\n": ["ret"], "/": ["shift", "7"], "-": ["slash"],
           "_": ["shift", "slash"], ":": ["shift", "dot"]}
SYMBOLS_US = {" ": ["spc"], ".": ["dot"], ",": ["comma"], "\n": ["ret"], "/": ["slash"], "-": ["minus"],
              "_": ["shift", "minus"], ":": ["shift", "semicolon"]}


def type_text(text, layout):
    sym = SYMBOLS if layout == "de" else SYMBOLS_US
    for ch in text:
        if ch.isalpha() and ch.isascii():
            k = ch.lower()
            if layout == "de":
                k = DE_SWAP.get(k, k)
            combo = (["shift"] if ch.isupper() else []) + [k]
        elif ch.isdigit():
            combo = [ch]
        elif ch in sym:
            combo = sym[ch]
        else:
            raise SystemExit(f"cannot type {ch!r}")
        qmp({"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": k} for k in combo]}})
        time.sleep(0.05)


def click(x, y):
    with open(os.path.join(DIR, "size")) as f:
        w, h = (int(v) for v in f.read().split())
    ax, ay = int(x * 32767 / max(w - 1, 1)), int(y * 32767 / max(h - 1, 1))
    ev = lambda down: {"execute": "input-send-event", "arguments": {"events": [
        {"type": "abs", "data": {"axis": "x", "value": ax}}, {"type": "abs", "data": {"axis": "y", "value": ay}},
        {"type": "btn", "data": {"down": down, "button": "left"}}]}}
    qmp({"execute": "input-send-event", "arguments": {"events": [
        {"type": "abs", "data": {"axis": "x", "value": ax}}, {"type": "abs", "data": {"axis": "y", "value": ay}}]}})
    time.sleep(0.1)
    qmp(ev(True))
    time.sleep(0.08)
    qmp(ev(False))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "start":
        start(args)
    elif cmd == "shot":
        shot(args[0])
    elif cmd == "key":
        keys(args)
    elif cmd == "type":
        layout = "us"
        if "--layout" in args:
            i = args.index("--layout")
            layout = args[i + 1]
            args = args[:i] + args[i + 2:]
        type_text(" ".join(args), layout)
    elif cmd == "click":
        click(int(args[0]), int(args[1]))
    elif cmd == "serial":
        print(open(os.path.join(DIR, "serial.log"), errors="replace").read())
    elif cmd == "stop":
        try:
            qmp({"execute": "quit"})
        except OSError:
            pass
        print("stopped")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
