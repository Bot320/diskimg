#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Build the diskimg rescue USB image (bootable ISO for USB sticks, UEFI and BIOS).
#
#   iso/build.sh [OUTPUT_DIR]            default: iso/out
#
# Needs archiso:  sudo pacman -S archiso
# Runs as a normal user (archiso then works in a user namespace; your user needs sub-UID/GID
# ranges in /etc/subuid and /etc/subgid, which Arch creates for new users) or as root.
#
# Contents: diskimg (built from this source tree) and BootRepair. BootRepair is taken from
# $BOOTREPAIR_PKG, else the newest bootrepair-*.pkg.tar.zst in ~/bootrepair. Without it the ISO
# is built without BootRepair.
# Work files go to $WORK_DIR (default ~/.cache/diskimg-iso, needs about 8 GB, not on tmpfs).
set -euo pipefail

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname -- "$here")
out=$(realpath -m -- "${1:-$here/out}")
user=${SUDO_USER:-$(id -un)}
user_home=$(getent passwd "$user" | cut -d: -f6)
work=${WORK_DIR:-$user_home/.cache/diskimg-iso}

command -v mkarchiso >/dev/null || die "archiso is not installed:  sudo pacman -S archiso"
if [ "$(id -u)" -ne 0 ]; then
    grep -q "^$user:" /etc/subuid && grep -q "^$user:" /etc/subgid \
        || die "no sub-UID/GID range for $user in /etc/subuid / /etc/subgid (or run as root)"
fi

as_user() {      # makepkg refuses to run as root
    if [ "$(id -u)" -eq 0 ]; then
        [ "$user" != root ] || die "makepkg cannot run as root: run this script as a normal user"
        sudo -u "$user" -- "$@"
    else
        "$@"
    fi
}

in_ns() {        # files created by an unprivileged build belong to sub-UIDs: remove them inside the namespace
    if [ "$(id -u)" -eq 0 ]; then "$@"; else unshare --map-auto --map-root-user -- "$@"; fi
}

mkdir -p "$out" "$work"
fstype=$(findmnt -n -o FSTYPE -T "$work" || true)
[ "$fstype" != tmpfs ] || warn "$work is on tmpfs: the build needs several GB of RAM there"
in_ns rm -rf -- "$work/build" "$work/profile"
rm -rf -- "$work/repo" "$work/makepkg"
mkdir -p "$work/repo"

say "Building the diskimg package"
as_user env PKGDEST="$work/repo" BUILDDIR="$work/makepkg" bash -c 'cd "$1" && makepkg -fd --noconfirm >/dev/null' \
    _ "$repo/packaging" || die "makepkg failed for diskimg"

br="${BOOTREPAIR_PKG:-}"
if [ -z "$br" ]; then
    br=$(ls -1 "$user_home"/bootrepair/bootrepair-*.pkg.tar.zst 2>/dev/null | sort -V | tail -n 1 || true)
fi
cp -a -- "$here/profile" "$work/profile"
if [ -n "$br" ] && [ -f "$br" ]; then
    say "Adding BootRepair: $(basename -- "$br")"
    cp -- "$br" "$work/repo/"
else
    warn "no BootRepair package found (set BOOTREPAIR_PKG): building without BootRepair"
    sed -i '/^bootrepair$/d' "$work/profile/packages.x86_64"
fi

say "Creating the local package repository"
repo-add -q "$work/repo/rescue-local.db.tar.gz" "$work"/repo/*.pkg.tar.zst
cat >> "$work/profile/pacman.conf" <<EOF

# packages built for this ISO (diskimg, BootRepair)
[rescue-local]
SigLevel = Optional TrustAll
Server = file://$work/repo
EOF

say "Building the ISO (this takes 10-30 minutes)"
log="$out/build.log"
if ! mkarchiso -v -r -w "$work/build" -o "$out" "$work/profile" > "$log" 2>&1; then
    tail -n 30 "$log" >&2
    die "mkarchiso failed - full log: $log"
fi
in_ns rm -rf -- "$work/build" "$work/profile"
iso=$(ls -1t "$out"/diskimg-rescue-*.iso | head -n 1)
[ "$(id -u)" -eq 0 ] && chown "$user": "$iso" "$log"
(cd "$out" && sha256sum "$(basename -- "$iso")" > "$(basename -- "$iso").sha256")
say "Done: $iso ($(du -h "$iso" | cut -f1))"
cat <<EOT
    Write it to a USB stick (ALL DATA ON THE STICK IS LOST; check the device name with lsblk):
        sudo dd if="$iso" of=/dev/sdX bs=4M conv=fsync oflag=direct status=progress
    or copy the file onto a Ventoy stick.
    Secure Boot must be switched off in the firmware settings of the PC.
EOT
