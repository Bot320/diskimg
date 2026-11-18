#!/usr/bin/env bash
# shellcheck disable=SC2034
# SPDX-License-Identifier: GPL-3.0-only
# archiso profile of the diskimg rescue USB (based on the official releng profile)

iso_name="diskimg-rescue"
iso_label="RESCUE_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="diskimg <https://github.com/Bot320/diskimg>"
iso_application="diskimg rescue USB: backups, Windows boot repair, partitions"
iso_version="$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y.%m.%d)"
install_dir="arch"
bootmodes=('bios.syslinux'
           'uefi.systemd-boot')
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-Xdict-size' '1M')
bootstrap_tarball_compression=('zstd' '-c' '-T0' '--auto-threads=logical' '--long' '-19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/etc/sudoers.d/10-live"]="0:0:440"
  ["/root"]="0:0:750"
  ["/root/.gnupg"]="0:0:700"
  ["/home/live/"]="1000:1000:750"
  ["/usr/local/bin/choose-mirror"]="0:0:755"
  ["/usr/local/bin/rescue-launcher"]="0:0:755"
  ["/usr/local/bin/rescue-root-app"]="0:0:755"
  ["/usr/local/bin/rescue-session"]="0:0:755"
  ["/usr/local/bin/rescue-text-menu"]="0:0:755"
)
