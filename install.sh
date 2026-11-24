#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# diskimg installer and updater (Arch Linux, also the Arch live ISO).
#
#   Install or update (same command):
#     curl -fsSL https://raw.githubusercontent.com/Bot320/diskimg/main/install.sh | sudo bash
#   Update later:      diskimg --update
#   Uninstall:         sudo bash /opt/diskimg/install.sh --uninstall
#   From a checkout:   sudo bash install.sh --local
#
# Installs to /opt/diskimg, links /usr/local/bin/diskimg, adds a menu entry, an icon and the polkit
# rule for the GUI helper, and installs the needed Arch packages with pacman (--needed).
# Settings (/etc/diskimg), mapfiles (/var/lib/diskimg) and logs (/var/log/diskimg) are never removed.
#
# Packaging/testing: DESTDIR=/some/dir stages all files below that directory, and
# DISKIMG_SKIP_PACKAGES=1 skips pacman.
set -euo pipefail

REPO="Bot320/diskimg"
BRANCH="${DISKIMG_BRANCH:-main}"
PREFIX="/opt/diskimg"
BIN="/usr/local/bin/diskimg"
APPID="io.github.bot320.diskimg"
DESKTOP="/usr/share/applications/$APPID.desktop"
ICON="/usr/share/icons/hicolor/scalable/apps/$APPID.svg"
POLICY="/usr/share/polkit-1/actions/$APPID.policy"

D="${DESTDIR:-}"          # staging root (empty for a normal install)

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

mode=install
local_src=""
for a in "$@"; do
    case "$a" in
        --update|--install) mode=install ;;
        --uninstall) mode=uninstall ;;
        --local) local_src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) ;;
        -h|--help) sed -n '3,14p' "${BASH_SOURCE[0]}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $a" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run as root, e.g.:  curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh | sudo bash"

# ---------------------------------------------------------------- uninstall
if [ "$mode" = uninstall ]; then
    say "Removing diskimg"
    rm -f "$D$BIN" "$D$DESKTOP" "$D$ICON" "$D$POLICY"
    rm -rf "$D$PREFIX" "$D$PREFIX.new" "$D$PREFIX.old"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
    say "Done. Kept: /etc/diskimg (saved shares, restic passwords), /var/lib/diskimg, /var/log/diskimg."
    echo "    Delete them yourself if you no longer need them (the restic passwords open your file backups!)."
    exit 0
fi

# ---------------------------------------------------------------- packages
live=0
[ -d /run/archiso ] && live=1
pkgs=(python python-textual python-rich ddrescue gptfdisk partclone ntfs-3g ntfsprogs zstd gnupg cifs-utils nfs-utils
      restic fuse3 python-pyfuse3 python-trio util-linux e2fsprogs curl tar)
# the GUI is skipped on the live ISO (no desktop there, and RAM is precious) unless asked for
if [ "$live" -eq 0 ] || [ "${DISKIMG_GUI:-0}" = 1 ]; then
    pkgs+=(python-gobject gtk4 libadwaita polkit)
fi
if ! python3 -c 'import compression.zstd' >/dev/null 2>&1; then
    pkgs+=(python-zstandard)
fi

if [ "${DISKIMG_SKIP_PACKAGES:-0}" = 1 ]; then
    say "Skipping package installation (DISKIMG_SKIP_PACKAGES=1)"
elif command -v pacman >/dev/null 2>&1; then
    missing=()
    for p in "${pkgs[@]}"; do
        pacman -Qq "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        say "Installing packages: ${missing[*]}"
        if [ "$live" -eq 1 ]; then
            avail_kb=$(df --output=avail -k / 2>/dev/null | tail -n 1 | tr -d ' ')
            if [ -n "$avail_kb" ] && [ "$avail_kb" -lt 800000 ]; then
                say "Live ISO: enlarging the RAM overlay to 2G"
                mount -o remount,size=2G /run/archiso/cowspace || warn "could not enlarge the overlay"
            fi
            # -Sy without -u is a partial upgrade: acceptable only on the live ISO (discarded at reboot)
            pacman -Sy --needed --noconfirm "${missing[@]}" || die "pacman failed"
        else
            if ! pacman -S --needed --noconfirm "${missing[@]}"; then
                die "pacman failed. Update the system first (pacman -Syu), then run this installer again."
            fi
        fi
    else
        say "All needed packages are installed"
    fi
else
    warn "pacman not found: install these packages yourself: ${pkgs[*]}"
fi

# ---------------------------------------------------------------- files
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -n "$local_src" ]; then
    say "Using the local copy in $local_src"
    src="$local_src"
else
    say "Downloading diskimg ($REPO, branch $BRANCH)"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$work/src.tar.gz" \
        || die "download failed"
    mkdir "$work/src"
    tar -xzf "$work/src.tar.gz" -C "$work/src" --strip-components=1 --no-same-owner \
        || die "the download is damaged"
    src="$work/src"
fi
for f in diskimg/engine.py diskimg/tui.py diskimg/gui.py diskimg/helper.py libexec/diskimg-run \
         libexec/diskimg-helper bin/diskimg install.sh data/$APPID.desktop data/$APPID.policy data/$APPID.svg; do
    [ -f "$src/$f" ] || die "incomplete source: $f is missing"
done
new_version=$(/usr/bin/python3 -I "$src/libexec/diskimg-run" version 2>/dev/null | awk '{print $2}') \
    || die "the new version does not start (python: $(python3 --version 2>&1))"
old_version=""
[ -x "$D$PREFIX/bin/diskimg" ] && old_version=$("$D$PREFIX/bin/diskimg" --version 2>/dev/null | awk '{print $2}' || true)

say "Installing diskimg $new_version to $D$PREFIX"
P="$D$PREFIX"
rm -rf "$P.new"
mkdir -p "$P.new"
for d in diskimg libexec bin data; do
    cp -r "$src/$d" "$P.new/"
done
cp "$src/install.sh" "$P.new/"
[ -f "$src/README.md" ] && cp "$src/README.md" "$P.new/"
[ -f "$src/LICENSE" ] && cp "$src/LICENSE" "$P.new/"
find "$P.new" -name '__pycache__' -prune -exec rm -rf {} +
# everything root-owned and not writable by others: the helper runs as root
chown -R root:root "$P.new"
find "$P.new" -type d -exec chmod 755 {} +
find "$P.new" -type f -exec chmod 644 {} +
chmod 755 "$P.new/bin/diskimg" "$P.new/libexec/diskimg-run" "$P.new/libexec/diskimg-helper" "$P.new/install.sh"
/usr/bin/python3 -m compileall -q "$P.new/diskimg" >/dev/null 2>&1 || true

# swap in atomically (a running diskimg keeps using the old files until it exits)
rm -rf "$P.old"
[ -d "$P" ] && mv "$P" "$P.old"
mv "$P.new" "$P"
rm -rf "$P.old"

mkdir -p "$(dirname "$D$BIN")"
ln -sfn "$PREFIX/bin/diskimg" "$D$BIN"
install -Dm644 "$P/data/$APPID.desktop" "$D$DESKTOP"
install -Dm644 "$P/data/$APPID.svg" "$D$ICON"
sed "s|@PREFIX@|$PREFIX|g" "$P/data/$APPID.policy" > "$work/policy"
install -Dm644 "$work/policy" "$D$POLICY"
if [ -z "$D" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
fi

if [ -n "$old_version" ] && [ "$old_version" != "$new_version" ]; then
    say "Updated diskimg $old_version -> $new_version"
elif [ -n "$old_version" ]; then
    say "diskimg $new_version reinstalled (already the newest version)"
else
    say "diskimg $new_version installed"
fi
cat <<EOT
    Start:      diskimg            (graphical on a desktop, text interface otherwise)
                diskimg --tui      (text interface, e.g. on the live ISO or over SSH)
                or "diskimg" in the application menu
    Update:     diskimg --update   (or run the install command again)
    Uninstall:  sudo bash $PREFIX/install.sh --uninstall
EOT
