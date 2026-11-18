# Rescue USB: start the graphical start screen on the first console
[ -f ~/.bashrc ] && . ~/.bashrc
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = /dev/tty1 ]; then
    exec /usr/local/bin/rescue-session
fi
