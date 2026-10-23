#!/bin/sh
# Installs pulled code on a device whose account has no passwordless sudo, so
# `make self-update` can still finish unattended. Run by `make redeploy` when
# `sudo -n` is refused; with sudo, redeploy re-renders the systemd units too.
#
# The daemon and the boot screen run straight from the checkout (editable
# install), so pulled Python code only needs a daemon restart. That restart
# needs no root either: the service runs as this same account, and killing it
# makes systemd's Restart=on-failure start it again on the new code. The
# player saves its position when the updater publishes `installing`, so the
# movie resumes after the restart.
#
# What this cannot refresh without root: system packages, the systemd units
# and the Wi-Fi scripts copied to /usr/local/lib. It names any such changes
# so a later `make pi` (run with the sudo password) can apply them.
set -u

PI_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$PI_DIR" || exit 1
GIT_DIR=$(git rev-parse --git-dir)
SERVICE=magicboxie-player
echo "redeploy: no passwordless sudo - installing without root"

# Reinstall into the venv only when the package definition changed.
stamp=.venv/.magicboxie-pyproject
want=$(git rev-parse HEAD:pi/pyproject.toml 2>/dev/null || git hash-object pyproject.toml)
if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
    echo "redeploy: pyproject.toml changed - reinstalling into the venv"
    .venv/bin/pip install -q -e . || { echo "redeploy: pip install failed - will retry on the next update"; exit 1; }
    printf '%s\n' "$want" >"$stamp"
fi

installed=$(cat "$GIT_DIR/magicboxie-installed-revision" 2>/dev/null)
if [ -n "$installed" ] && git cat-file -e "$installed^{commit}" 2>/dev/null; then
    root_changes=$(git diff --name-only "$installed" HEAD -- system Makefile \
        player_app/wifi_startup.py player_app/wifi_networks.py player_app/storage.py)
    if [ -n "$root_changes" ]; then
        echo "redeploy: these need root and were not applied - run 'make pi' with the sudo password:"
        echo "$root_changes" | sed 's/^/  /'
    fi
fi

pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
if [ -z "$pid" ] || [ "$pid" = 0 ]; then
    echo "redeploy: $SERVICE is not running - it starts on the new code next time"
    exit 0
fi
echo "redeploy: restarting $SERVICE (pid $pid) on the new code"
kill -KILL "$pid" || { echo "redeploy: could not stop $SERVICE"; exit 1; }
# Restart=on-failure brings it back after RestartSec; confirm it did.
i=0
while [ $i -lt 30 ]; do
    sleep 1
    new=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
    if [ -n "$new" ] && [ "$new" != 0 ] && [ "$new" != "$pid" ]; then
        echo "redeploy: $SERVICE is back (pid $new)"
        exit 0
    fi
    i=$((i + 1))
done
echo "redeploy: $SERVICE did not come back within 30 seconds"
exit 1
