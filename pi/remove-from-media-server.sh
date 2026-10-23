#!/bin/sh
# Removes a player install from the home media server Pi (magicboxie-mediaserver).
# The player belongs only on the Pi Zero in the car. In October 2026 it was
# installed on the media server by mistake, which renamed that Pi to
# magicboxie-player, turned off its nginx, and replaced its
# magicboxie-self-update units (both repos use that name) with the player's.
#
# Run by `make self-update` when it finds itself on the media server, so a
# stray install cleans itself up on its next update; or by hand with
# `make remove-from-media-server`. Touches only what the player installed,
# restores the media server's own self-update units, hostname and nginx, and
# deletes the player checkout last.
set -u

is_media_server() { [ -e /etc/magicboxie/docker.yaml ] || [ -d /var/lib/magicboxie-docker ]; }
if ! is_media_server; then
    echo "remove-from-media-server: this is not the media server - nothing to do"
    exit 0
fi

PI_DIR=$(cd "$(dirname "$0")" && pwd)
CHECKOUT=$(git -C "$PI_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$PI_DIR")
echo "remove-from-media-server: removing the player from this media server ($CHECKOUT)"

for unit in magicboxie-player.service magicboxie-device.service magicboxie-splash.service \
    magicboxie-wifi-startup.service magicboxie-wifi-retry.timer magicboxie-wifi-retry.service \
    magicboxie-wifi-search.service magicboxie-hotspot.service magicboxie-apt-update.timer \
    magicboxie-apt-update.service magicboxie-boot-update.service; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$unit"
done

# The self-update units are the player's when they run make (the media
# server's run scripts/pi-self-update.sh). Not stopped: this may be running
# inside that very service.
if grep -q 'make -C' /etc/systemd/system/magicboxie-self-update.service 2>/dev/null; then
    sudo systemctl disable magicboxie-self-update.timer magicboxie-self-update.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/magicboxie-self-update.service /etc/systemd/system/magicboxie-self-update.timer
    for server in /home/*/magicboxie-web /home/*/magicboxie-mediaserver; do
        units="$server/deploy/pi/systemd"
        [ -f "$units/magicboxie-self-update.service.in" ] || continue
        echo "remove-from-media-server: restoring the media server's self-update from $server"
        sed -e "s|@REPO_DIR@|$server|g" "$units/magicboxie-self-update.service.in" |
            sudo tee /etc/systemd/system/magicboxie-self-update.service >/dev/null
        sudo install -m 0644 "$units/magicboxie-self-update.timer" "$units/magicboxie-self-update.path" /etc/systemd/system/
        break
    done
fi
sudo systemctl daemon-reload
if [ -f /etc/systemd/system/magicboxie-self-update.timer ]; then
    sudo systemctl enable --now magicboxie-self-update.timer
    [ -f /etc/systemd/system/magicboxie-self-update.path ] && sudo systemctl enable --now magicboxie-self-update.path
fi
sudo systemctl reset-failed 2>/dev/null || true

sudo nmcli connection delete magicboxie-hotspot 2>/dev/null || true
sudo rm -rf /etc/sudoers.d/magicboxie /etc/magicboxie-hotspot-dnsmasq.conf /usr/local/lib/magicboxie \
    /var/lib/magicboxie-hotspot /run/magicboxie-boot-update.json \
    /var/lib/magicboxie/thumbnails /var/lib/magicboxie/transcoded \
    /var/lib/magicboxie/wifi-networks.json /var/lib/magicboxie/home-server.env
sudo rm -f "/content/Sample Feature Film.mp4" "/content/Sample TV Episode.mp4" "/content/Sample Clip.mp4"
[ -f /boot/firmware/cmdline.txt ] && sudo sed -i 's/ video=HDMI-A-1:720x480@60//' /boot/firmware/cmdline.txt

# The player's install turns nginx off to free port 80; the media server needs it.
if [ -e /etc/nginx/sites-enabled/magicboxie ]; then
    sudo systemctl enable --now nginx || true
fi
if [ "$(hostname)" = magicboxie-player ]; then
    echo "remove-from-media-server: renaming this Pi back to magicboxie"
    sudo hostnamectl set-hostname magicboxie
    sudo sed -i 's/\bmagicboxie-player\b/magicboxie/g' /etc/hosts
    sudo systemctl restart avahi-daemon 2>/dev/null || true
fi

# Last, since this script lives in it: the player checkout (and an older one
# under its previous name).
for dir in "$CHECKOUT" "${HOME:-/nonexistent}/magicboxie-player" "${HOME:-/nonexistent}/magicboxie-device"; do
    if [ -d "$dir/pi/player_app" ]; then
        echo "remove-from-media-server: deleting player checkout $dir"
        rm -rf "$dir" 2>/dev/null || sudo rm -rf "$dir"
    fi
done
echo "remove-from-media-server: done"
