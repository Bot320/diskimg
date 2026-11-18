# SPDX-License-Identifier: GPL-3.0-only
"""diskimg - disk imaging for Arch Linux: raw and filesystem-aware images (ddrescue, partclone),
zstd compression, gpg encryption, SMB/NFS shares, instant browsing, restic file backups.

Modules: engine (no user interface), tui (Textual, runs as root), helper (root backend for the
GUI, started via pkexec), gui (GTK4/libadwaita, runs as the desktop user)."""
