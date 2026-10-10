# Deploying MagicBoxie Player

Production runs directly on Raspberry Pi OS using systemd and a Python virtual
environment. Docker is for local development. Run the `pi-*` Make targets on
the Pi as the device account, with `sudo` access, from the project checkout.

## Device access and current installation

The development Mac's SSH alias `pi` resolves to `192.168.86.27`, hostname
`magicboxie`, account `admin`. The user identified this machine as the **home
server**, not the target device. Do not deploy device changes through that
alias until it has been pointed at the correct Pi.

On October 3, 2026, the device was verified over password-authenticated SSH:

| Item | Value |
| --- | --- |
| LAN address | `192.168.86.57` |
| SSH account | `admin` |
| Hardware | Raspberry Pi Zero 2 W Rev 1.0 |
| Hostname | `magicboxie-player` (`magicboxie-player.local`) |
| Checkout | `/home/admin/magicboxie-device` |
| Connection | Ethernet (`eth0`) |
| Daemon | `magicboxie-player.service`, active |
| Open ports checked | SSH 22 and device HTTP API 8000 |

Connect using `ssh admin@192.168.86.57` and the supplied device password.
Key authentication was not available. Replace `DEVICE_USER` in the commands
below with `admin` and `DEVICE_IP` with `192.168.86.57` (or its current address).
The API's `/api/version` endpoint reports that same LAN address. Port 80 was
closed, and the Wi-Fi startup and self-update units were not installed yet.
The OS detected an Apple Magic Keyboard with Numeric Keypad at
`/dev/input/event0`; the daemon logged that it was watching for Escape.

Look for the service `MagicBoxieDevice._magicboxie._tcp.local.`. The home
server also advertised that name with an unusable `127.0.0.1` address; neither
the name nor a matching API response alone identifies the target device.
Verify the responding host and hardware over SSH before deploying.

The home server had a running `magicboxie-player.service` on port 8000 and
nginx on port 80. Its Wi-Fi startup and self-update units were absent. Those
observations describe the home server, not the other Pi. The target's current service state is listed above.

Changes present only in a development working tree cannot reach the Pi through
`git pull`; commit and push the intended release before using Git deployment.

## Prerequisites

Use Raspberry Pi OS with NetworkManager, an AP-capable `wlan0`, and internet
access for Git, apt, and pip. Legacy dhcpcd/hostapd setups are not migrated by
the installer. Keep an Ethernet connection or local console available when
changing Wi-Fi configuration.

Check port ownership before deploying the portal:

```sh
sudo ss -ltnp 'sport = :80'
sudo ss -ltnp 'sport = :8000'
```

The new daemon listens on both ports 80 and 8000. If another service owns port 80, move or reconfigure that listener before
starting the new portal-enabled daemon; installation does not resolve this
conflict. Nginx was observed on the home server, so check the target separately.
The existing MagicBoxie daemon owning port 8000 is expected and is replaced
by restarting its service.

The unattended updater runs as the installing user and invokes `sudo` during
redeployment. Verify that account can run the required deployment commands
without a password prompt; interactive sudo access alone is insufficient.

## First installation or upgrading an older sparse checkout

On the Pi, run:

```sh
curl -fsSL https://raw.githubusercontent.com/kriogenx0/magicboxie-device/main/install.sh | sh
```

The bootstrap script defaults to branch `main` and `~/magicboxie-player`.
`MAGICBOXIE_REF` and `MAGICBOXIE_INSTALL_DIR` can override these defaults.
It installs Git if needed and creates a shallow sparse checkout containing
`pi/` (everything deployed to the device) and the root `Makefile`, which
forwards `pi-*` targets to `pi/Makefile`. It then runs `make pi-install`.
The service, virtualenv, and updater all run from `pi/` in the checkout.

On an existing checkout, bootstrap fetches the selected branch and resets
tracked files to the remote version. Preserve local edits before rerunning
it. Runtime movies and Wi-Fi credentials live outside the checkout.
Reusing the new bootstrap also expands older sparse checkouts to include
all `pi/system` files required by the new services.
If the daemon was already running, follow installation with `make pi-restart`:
`pi-install` uses `systemctl start`, which does not restart an active daemon.

```sh
cd ~/magicboxie-player
make pi-restart
```

For an already complete checkout of the intended revision, use:

```sh
cd ~/magicboxie-player
make pi-install
```

Installation runs these steps in order:

1. Install system dependencies, create/update `.venv`, and install the app.
2. Create content/cache directories and seed sample movies if `/content`
   contains no MP4 files. This can take time on a Pi Zero.
3. Render and enable the daemon and daily update timer.
4. Install the Wi-Fi startup script, open AP profile, and dedicated DNS/DHCP
   service; enable the startup policy.
5. Start the daemon, then queue the Wi-Fi startup check.

Wi-Fi selection is queued asynchronously; installer completion does not mean
that the 30-second window has finished. Inspect the startup logs to verify it.
Use these Make targets without `-j`, since installation order matters.

## Deploy a published update

After pushing the intended changes, connect to the Pi and run:

```sh
ssh DEVICE_USER@DEVICE_IP
cd ~/magicboxie-player
make pi
```

`make pi` pulls the current branch, refreshes packages and the virtual
environment, renders the service definitions, restarts the daemon, and queues
Wi-Fi selection. It restarts immediately, so schedule manual deployment when
playback can be interrupted.

To install an update through the playback-aware updater instead:

```sh
sudo systemctl start --no-block magicboxie-self-update.service
journalctl -u magicboxie-self-update -f
```

For code already present in the checkout, `make pi-redeploy` refreshes the
daemon and Wi-Fi installation and restarts them without pulling. It does not
install the update timer; use `make pi-install` or `make pi` when introducing
that service to an older device.

## Saved Wi-Fi and startup behavior

The tracked seed file is `pi/system/wifi-networks.json`. The installer copies it
to `/var/lib/magicboxie/wifi-networks.json` only if the runtime file is absent.
Existing runtime credentials survive installation, Git updates, and reboots.
The runtime file has mode `600` and belongs to the device account. The tracked
file may include credentials, as authorized for this project.

Edit an existing device's credentials with:

```sh
sudoedit /var/lib/magicboxie/wifi-networks.json
```

The format is:

```json
{
  "version": 1,
  "networks": [
    {"ssid": "Mitera", "password": "YOUR_MITERA_PASSWORD"},
    {"ssid": "AV-iPhone17Pro", "password": "YOUR_IPHONE_HOTSPOT_PASSWORD"}
  ]
}
```

Replace these placeholders with actual passwords. The tracked file currently
has an empty network list. An empty password means an open network, not an
unknown password. Successful BLE provisioning also saves credentials to the
runtime file. Profiles created manually with `nmcli` are not exported to JSON.

On boot, `magicboxie-wifi-startup.service` loads the JSON entries into stable
NetworkManager profiles, then allows up to 30 seconds for `wlan0` to connect.
Existing saved NetworkManager profiles with autoconnect enabled also work.

- Saved Wi-Fi connects: keep that connection and queue a self-update attempt.
  Wi-Fi does not need internet access to count as connected.
- No Wi-Fi connects within 30 seconds: activate **MagicBoxie Player**, with
  no password, at `10.42.0.1`. Ethernet alone does not suppress this fallback.
- Hotspot already active during deployment: preserve it and its clients.

The hotspot provides an offline captive portal at `http://10.42.0.1/` and the
existing API at `http://10.42.0.1:8000`. If the client does not open the login
page automatically, open the HTTP URL manually. Anyone within Wi-Fi range
can access the device's unauthenticated controls and API.

Startup chooses once; it does not continuously switch between saved Wi-Fi
and hotspot mode. BLE provisioning can switch the adapter to a supplied
network. To rerun startup selection, use `make pi-wifi-start` or reboot.
Editing the tracked seed does not replace an existing runtime file. A network
removed from the settings page is recorded under `forgotten` in the runtime
file, so later deploys do not re-add it from the seed; startup and **Find
Wi-Fi networks** then delete `magicboxie-saved-*` NetworkManager profiles
whose network is no longer saved.

### Searching for Wi-Fi from settings

The Pi has one Wi-Fi radio, so it cannot broadcast the hotspot and scan for
networks at once. The web page's settings (gear icon) has a **Networks** tab
showing the current connection (Wi-Fi name or hotspot, signal, IP addresses,
internet) and the saved networks, plus **Find Wi-Fi networks**, which runs `magicboxie-wifi-search.service`:

1. Stops the hotspot and brings its access point down (clients, including the
   phone using the page, disconnect).
2. Rescans and gives saved Wi-Fi up to 30 seconds to connect.
3. Connected: keeps that connection and queues a self-update.
4. Not connected: starts the hotspot again; reconnect to **MagicBoxie Player**.

To add a network without BLE or SSH (for example an iPhone Personal Hotspot),
use **Saved Wi-Fi networks** on the Networks tab: enter the name and
password and tap **Save network**; **Remove** forgets one. This only saves it (the page stays
connected to the hotspot); the saved network is joined at the next boot or by
**Find Wi-Fi networks**. Saved names are listed, passwords never are
(`GET/POST/DELETE /api/wifi/networks`, `GET /api/network`).

The web service starts the unit with `sudo -n systemctl start --no-block
magicboxie-wifi-search.service`, allowed by `/etc/sudoers.d/magicboxie`
(written by the `sudoers` Makefile target, run by `make pi-setup` and
`make pi-wifi-service`). Until a device has run one of those, the button
reports that it is not permitted.

## Service overview

| Unit | Type | Role |
| --- | --- | --- |
| `magicboxie-wifi-startup` | oneshot, at boot | Restores saved profiles; 30 s for saved Wi-Fi, else starts the hotspot |
| `magicboxie-hotspot` | long-running | Open AP `MagicBoxie Player` (10.42.0.1) plus dnsmasq for DHCP and captive-portal DNS |
| `magicboxie-player` | long-running | The daemon: mpv, BLE, web/API, mDNS, temperature check, home-server sync |
| `magicboxie-boot-update` | oneshot, at boot | Polls for internet up to 30 s at lowest CPU/IO priority, then queues a self-update |
| `magicboxie-self-update` | oneshot | Pulls and, if there is new code, installs it at once; started by startup, boot-update and the timer |
| `magicboxie-self-update.timer` | timer | Daily run, up to 1 h random delay, catches up missed runs |
| `magicboxie-apt-update.timer` | timer | Monthly `apt-get update` in the background at idle priority (not part of updates, which would wait on it) |
| `magicboxie-wifi-search` | oneshot, on demand | Settings-menu Wi-Fi search described above |

Progress text ("Checking for internet…", update phases) travels from the
update processes to the daemon through small JSON status files
(`player_app/update_status.py`) and appears as a banner on the idle screen.

## Boot playback

The last played movie and its exact position are kept whether it was playing,
paused or stopped, and across updates and reboots. A movie that was playing
or paused (including one the player quit on with Escape, or one an update
stopped) resumes at boot; a paused one comes back paused on its last frame.
A movie stopped from the web page or the app does not resume at boot: the
device starts on the idle screen, where its slide shows a progress bar and
selecting it resumes at the saved position. Only a movie that played to its
end, failed, or no longer exists is forgotten.

## Idle screen

From power-on the boot loader (`splash.py`) shows the MB logo (the apps' mark) above
three animated dots until the player puts its first picture up, and brings the logo back
whenever the player restarts (after a self-update or a crash).

On the 720x480 TV output the idle screen shows, in order of priority:

1. **Updating** while a software update installs; the logo follows while the
   player restarts.
2. **Downloading** a movie from the media server, with a progress bar and how
   many more are queued.
3. **Transcoding** on the media server: a movie this player is waiting for,
   while the media server is reachable.
4. Otherwise the poster slideshow. Movies stopped partway have a red
   progress bar along the bottom of the poster.

The activity screens refresh their progress with each slideshow tick.

After a reboot the daemon resumes the last movie at its saved position as
fast as it can. It does not draw the idle screen at startup, since rendering
it competes for CPU with loading the movie; the idle screen is drawn only
when nothing is resumed (or a pending update blocks playback). Internet and
update checks run in parallel in the background: `magicboxie-boot-update`
tries for the first 30 seconds at idle priority, and never delays playback.
An update installs as soon as it is found: the daemon stops the movie, the
install runs, and the restarted daemon resumes the movie at the same position.

## Escape on the device keyboard

Pressing Escape on a USB keyboard attached to the device quits the player app
cleanly (exit code 0), which frees the HDMI screen for a console login. The
service uses `Restart=on-failure`, so systemd does not bring it back. It stays
off, with the web page, BLE and mDNS, until the next reboot or a manual
`sudo systemctl start magicboxie-player`. A later self-update also restarts it.

## Boot loader screen

`magicboxie-splash.service` runs before the console login on tty1. It puts the
console in graphics mode (no text shows), blanks the screen and draws a small
spinner on the framebuffer until the player's mpv is up. It then hands the
screen to the player and waits. When the player has been stopped for three
seconds in a row (for example after Escape), it switches the console back to
text and the login prompt returns. A restart in the middle of a self-update
is too short to count.

- Escape on any attached keyboard during the loader returns to the prompt and
  stops `magicboxie-player`.
- If the framebuffer or player never appears, the loader gives up after two
  minutes and shows the prompt.
- Kernel boot text appears before this service starts. To hide it too, append
  `quiet loglevel=0 logo.nologo vt.global_cursor_default=0` to
  `/boot/firmware/cmdline.txt` by hand (one line, a mistake can stop the Pi
  booting, so this is not done automatically).
- Installed by `make pi-wifi-service` (and so by install/deploy). The unit
  runs `pi/player_app/splash.py` from the checkout, so a self-update changes
  the boot screen on the next boot without reinstalling anything. Devices set
  up before this change still run an old copy from `/usr/local/lib/magicboxie/`
  until `make pi` runs once with sudo.

## Self-update lifecycle

The same `magicboxie-self-update.service` handles the request after saved
Wi-Fi connects and the daily timer. The timer uses a daily calendar schedule,
a randomized delay of up to one hour, and persistence for missed runs.

The updater:

1. Skips a checkout with local changes.
2. Pulls with `git pull --ff-only`. An offline or failed pull ends the attempt
   without disconnecting Wi-Fi; the next timer/startup request can retry.
3. Compares Git HEAD with `.git/magicboxie-installed-revision`. A missing or
   different marker requires installation, including retrying an interrupted
   deployment even if no new commits were pulled this time.
4. Publishes the `installing` status. The daemon sees it, saves the exact
   playback position, stops the movie and shows "Updating device software" on
   the idle screen, so the install has the CPU to itself.
5. Redeploys, restarts the daemon, and records the successfully installed
   revision. It clears the live update status when the updater exits.

There is no wait for playback to end: an update interrupts the movie, and the
restarted daemon resumes it where it stopped. Hotspot fallback does not request a startup update; the daily
timer remains enabled. Already running update requests use the same systemd
unit rather than launching parallel updater processes.

## Verification and operations

On the Pi:

```sh
systemctl status magicboxie-player magicboxie-wifi-startup
systemctl list-timers magicboxie-self-update.timer
nmcli -f NAME,TYPE,DEVICE connection show --active
curl -fsS http://localhost:8000/api/version
curl -fsS http://localhost:8000/api/status
curl -fsS http://localhost/
journalctl -u magicboxie-wifi-startup -b
journalctl -u magicboxie-self-update -n 100
```

`magicboxie-wifi-startup` normally becomes `active (exited)` after choosing a
network. `magicboxie-hotspot` should run only when AP mode is in use. The
self-update service can be inactive between attempts; check its logs and timer.

Use `make pi-logs` for live daemon logs, and `make pi-start`, `make pi-stop`,
or `make pi-restart` for routine service control. `make pi-uninstall` removes
the managed services and AP profile, while preserving movies and runtime
credentials. `make pi-clean` removes the virtual environment.

For discovery on macOS:

```sh
dns-sd -B _magicboxie._tcp local.
dns-sd -L MagicBoxieDevice _magicboxie._tcp local.
dns-sd -G v4v6 MagicBoxieDevice.local.
```

Stop these commands with Ctrl-C. Verify the advertised address is reachable;
`127.0.0.1` refers to the client itself. When discovery is wrong, use the
router's client list or a known LAN address, then confirm the systemd daemon
and its listener over SSH.

## Downloading movies from the home server

The home server's name (default `http://magicboxie.lan`) only resolves on the
Mitera network. The device tests whether it resolves every 15 seconds, so a
sync starts soon after joining Mitera (by Wi-Fi or Ethernet) and nothing is
attempted elsewhere. Once it resolves, the device checks in every minute and
downloads movies it does not have yet, one at a time. The player does no
transcoding of its own; the media server makes each 480p copy. Downloads run
during playback too, capped at 1 MB/s.

The server login is stored on the device in
`/var/lib/magicboxie/home-server.env` (mode `600`, outside Git), as
`MAGICBOXIE_HOME_SERVER_PASSWORD="..."`. It is seeded with the default
home server login (`123456`) the first time. Edit it with
`sudoedit /var/lib/magicboxie/home-server.env`, then run
`sudo systemctl restart magicboxie-player`. Updates and redeploys keep it. A
password passed once with `make pi-service HOME_SERVER_PASSWORD=...` is also
written there. Use `make pi-service HOME_SERVER_URL=...` to point at a
different server.
