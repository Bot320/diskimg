# diskimg

Disk imaging for Arch Linux, with a GTK 4 / libadwaita window and a text interface as fallback.

- **Disk → Image**: raw, bit-exact copies with GNU ddrescue (resumable, handles failing disks), or
  filesystem-aware copies that store only used data (partclone). Optional zstd compression and
  gpg (AES-256) encryption.
- **Disk → Disk**: clone a disk or a partition directly.
- **Image → Disk**: restore, including the partition table and boot area.
- **Check**: re-read a backup and compare every checksum.
- **Browse**: open the partitions of a backup read-only. Most backups open instantly, without
  unpacking (chunked format, served through FUSE).
- **File backup**: back up folders (Linux, or a Windows partition mounted read-only) with
  restic, browse every version, and optionally delete old versions.
- **SMB / NFS shares**: back up to or restore from a NAS.

Safety rules the program follows:

- A job is never reported as successful and verified while there are errors. A verification
  reads the data back from the disk itself, not from the page cache. Areas that were unreadable
  at backup time are reported, also when restoring or checking later.
- An existing backup is only replaced after the new one is complete. A previous backup
  stays as `NAME.old` until the new one is verified without unreadable areas.
- Devices are opened exclusively while a job runs, so they cannot be mounted at the same time.
  To overwrite a device you have to type its name.
- diskimg never creates a new, empty file-backup repository in place of a missing one (for
  example when the NAS is not connected).

## Install / update

```sh
curl -fsSL https://raw.githubusercontent.com/Bot320/diskimg/main/install.sh | sudo bash
```

Running the same command again updates diskimg. So does `diskimg --update`. The installer adds
the needed Arch packages with `pacman --needed`, installs to `/opt/diskimg`, and adds
`/usr/local/bin/diskimg`, a menu entry and a polkit rule.

On the Arch live ISO the same command works. It enlarges the RAM overlay if needed and leaves
out the GUI packages (set `DISKIMG_GUI=1` to include them).

Uninstall:

```sh
sudo bash /opt/diskimg/install.sh --uninstall
```

This keeps `/etc/diskimg` (saved shares and the **restic passwords of your file backups**),
`/var/lib/diskimg` and `/var/log/diskimg`.

## Use

```
diskimg              window on a desktop, text interface otherwise
diskimg --gui        window (runs as you; asks for the admin password once, via polkit)
diskimg --tui        text interface (runs as root; also over SSH / on the live ISO)
diskimg --extract DATA.chunks INDEX.cidx OUT.img   rebuild a partition image by hand
diskimg --version
```

Every backup folder (`*.dimg`) contains an `info.txt` that explains how to restore it by hand,
plus `SHA256SUMS`.

## How it is built

| Part | Runs as | Purpose |
|---|---|---|
| `diskimg/engine.py` | — | devices, image formats, verification, jobs (`JobRunner`); no UI code |
| `diskimg/tui.py` | root | Textual text interface |
| `diskimg/gui.py` | your user | GTK 4 / libadwaita window |
| `diskimg/helper.py` | root (pkexec) | backend of the window: JSON lines over a private pipe |

The window never runs as root. It starts `diskimg-helper` once through `pkexec` (polkit action
`io.github.bot320.diskimg.helper`, cached for a few minutes). It then sends requests such as
"list devices", "plan this job" or "start" over the helper's stdin/stdout. When the window
closes, the helper stops any running job, unmounts what it mounted and exits.

## Tests

No root needed. Regular files stand in for partitions.

```sh
python3 -I tests/test_jobs.py ~/diskimg-test     # backup / check / restore / damage detection (ext4)
python3 -I tests/test_helper.py BACKUP.dimg      # helper protocol
python3 -I tests/test_gui.py BACKUP.dimg         # GTK window, headless (gtk4-broadwayd)
python3 -I tests/test_tui.py BACKUP.dimg         # text interface, headless
```

## Requirements

Arch Linux with its current Python (tested with 3.14, which brings zstd; on older versions
`python-zstandard` is installed),
plus `python-textual`, `ddrescue`, `gptfdisk`, `partclone`, `ntfs-3g`, `zstd`, `gnupg`,
`cifs-utils`, `nfs-utils`, `restic`, `fuse3`, `python-pyfuse3`, and for the window
`python-gobject`, `gtk4`, `libadwaita` (≥ 1.6) and `polkit`. The installer handles all of this.

## License

Copyright © 2026 Bot320 and contributors.

diskimg is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License, version 3 (GPL-3.0-only), as published by the Free Software
Foundation. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See [LICENSE](LICENSE) for the full text.
