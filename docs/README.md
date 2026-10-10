# MagicBoxie Player

The MagicBoxie player daemon runs on a Raspberry Pi, plays video over HDMI,
and exposes BLE and local HTTP control surfaces. Production installs run
natively under systemd; Docker is only used for development.

See [deployment.md](deployment.md) for deployment commands, device discovery,
Wi-Fi configuration, self-update behavior, and verification.

## The two Raspberry Pis

MagicBoxie runs on two separate Raspberry Pis:

| | **Player** | **Media server (home cloud)** |
| --- | --- | --- |
| Repository | `magicboxie-player` (this repo) | `magicboxie-web` |
| Hardware | Raspberry Pi Zero 2 W | Raspberry Pi |
| Hostname | `magicboxie-player` (`magicboxie-player.local`) | `magicboxie` (`magicboxie.lan`, `magicboxie.local`) |
| Where it lives | In the car, playing on the Honda Pilot screen (see [Connection modes](#connection-modes)) | At home on the home network |
| Job | Plays movies, serves its own control page and API, BLE control from the iOS app | Imports, stores and transcodes the movie library; serves the web app |

The player is offline most of the time. Whenever it is on the home Wi-Fi it
checks in with the media server at `http://magicboxie.lan` and downloads any
movies it doesn't have yet (`pi/player_app/services/home_sync_service.py`).
Deploy each repo only to its own Pi: the media server also advertises a
`MagicBoxieDevice` service, so a name or API response alone does not tell the
two apart (see [deployment.md](deployment.md)).

## Install on a Raspberry Pi

Start with Raspberry Pi OS (or another Debian-based Pi installation), connect
the Pi to the internet, and open a terminal on it directly or over SSH. The
installing user must have `sudo` access.

Run the bootstrap installer:

```sh
curl -fsSL https://raw.githubusercontent.com/kriogenx0/magicboxie-device/main/install.sh | sh
```

The script:

- installs Git if necessary;
- creates a minimal checkout in `~/magicboxie-player`;
- installs the required system and Python packages;
- creates `/content` for movies and seeds it with sample videos when empty;
- installs and enables the `magicboxie-player` systemd service; and
- starts the service immediately; and
- tries saved Wi-Fi at startup and enables an open **MagicBoxie Player**
  Wi-Fi hotspot and captive portal if none connects within 30 seconds.

The install may take a while on a Pi Zero because it installs packages and
generates the sample videos.

### Connect to the device

Join **MagicBoxie Player** in your phone or computer's Wi-Fi settings. There
is no password. The captive portal opens the Pi's own webpage, where you can
browse movies and control playback on its connected screen. Everything is
served locally; the page requires no internet or external assets.

If the login page does not open automatically, choose the network's
“sign in” option or open **http://10.42.0.1/** in a browser. Portal popups
are controlled by the client OS; VPNs, private DNS, and disabled network
checks can prevent automatic opening. HTTPS sites cannot be redirected to
this HTTP portal without certificate errors; use the HTTP address above.

The hotspot uses `wlan0` in 2.4 GHz AP mode and gives clients addresses in
`10.42.0.0/24`. It is an offline network, without internet forwarding.
NetworkManager owns the open AP profile; a dedicated dnsmasq service provides
DHCP and maps DNS names to the Pi, and the device serves the webpage on port
80 alongside its existing API on port 8000. The network setup follows the
[NetworkManager keyfile documentation](https://networkmanager.dev/docs/api/latest/nm-settings-keyfile.html)
and [dnsmasq documentation](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html).

At every boot, NetworkManager first tries saved Wi-Fi profiles with automatic
connection enabled, such as **Mitera** or **AV-iPhone17Pro**. If `wlan0` has
not connected to a saved network within 30 seconds, the device starts its
open hotspot. A successful Wi-Fi connection keeps the hotspot off, even if
that network has no internet. Ethernet alone does not suppress the hotspot.
The hotspot profile itself has automatic connection disabled so it cannot
preempt the saved-network startup window. Profile activation uses
[NetworkManager's saved connections](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html).

Saved SSIDs and passwords live in **`/var/lib/magicboxie/wifi-networks.json`**,
outside the Git checkout. The installer copies the tracked **`pi/system/wifi-networks.json`** only if the
device file does not already exist, preserves it across updates, and sets permissions to `600`
(owner and root only). Every deploy and update also adds any network in the tracked file whose
SSID the device file lacks, without changing existing entries or their passwords. Successful BLE provisioning updates this file atomically.
Startup restores these entries into NetworkManager before trying saved Wi-Fi.
Existing NetworkManager-only profiles also continue to work.

Edit the file on the Pi with `sudoedit /var/lib/magicboxie/wifi-networks.json`:

```json
{
  "version": 1,
  "networks": [
    {"ssid": "Mitera", "password": "YOUR_MITERA_PASSWORD"},
    {"ssid": "AV-iPhone17Pro", "password": "YOUR_IPHONE_HOTSPOT_PASSWORD"}
  ]
}
```

Replace the example passwords with the real ones. An empty password represents
an open network; protected entries use WPA personal passwords. The static
`pi/system/wifi-networks.json` may contain credentials and be committed, as
authorized. It currently contains an empty list because no credentials have
been supplied. Editing the tracked file seeds new installations; to update an
existing device, edit its runtime file as shown above. Manually creating profiles with `nmcli` does not update the JSON
file; use the file or BLE when you want both stores to stay synchronized.
Removing an entry from the JSON does not delete its existing NetworkManager
profile; also delete that profile if you want to forget the network.

Once startup connects to saved Wi-Fi, it immediately requests the existing
self-update service. The update runs in the background, pulls the latest code,
and installs any changes right away (a playing movie is stopped and resumes
at the same position after the restart). A failed update
or a network without internet leaves the Wi-Fi connection intact. The daily
update timer remains enabled for later retries. Hotspot fallback does not
request an update.

The startup check also runs at the end of installation and deployment. An
existing Wi-Fi connection or active hotspot stays connected. If fallback
activates, connect to **MagicBoxie Player**, then SSH to `10.42.0.1` with your
existing Pi account. Use Ethernet or a second Wi-Fi adapter for internet
access while broadcasting. BLE provisioning can switch the built-in adapter
to a supplied network. While the hotspot is up, a timer checks every five
minutes: if nobody is connected to the hotspot, it turns the hotspot off for
another 30-second saved-Wi-Fi window, so the device returns to a saved network
such as Mitera once it is back in range (and requests an update, as at
startup). If no saved network connects, the hotspot comes back. Anyone
connected to the hotspot keeps it up. Reboot or run `make pi-wifi-start` to
try the startup policy immediately.

Anyone in Wi-Fi range can join and use the device's unauthenticated controls
and API, including uploads and deletions. Use this mode where that access is
intended. The installer targets Raspberry Pi OS with NetworkManager and an
AP-capable `wlan0`; it does not migrate legacy dhcpcd/hostapd installations.

### Verify the installation

Check the service:

```sh
systemctl status magicboxie-player
```

Follow its logs:

```sh
cd ~/magicboxie-player
make pi-logs
```

The local HTTP API listens on port 8000. Confirm it is responding from the Pi:

```sh
curl http://localhost:8000/api/version
```

Press `Ctrl-C` to stop following logs; this does not stop the service.

## Connection modes

The player runs in one of two modes, chosen when it starts:

| | **Car mode** (default) | **Debug mode** |
| --- | --- | --- |
| Setup | Honda Pilot rear-seat screen, fed through an HDMI-to-composite converter | Plugged into a computer monitor or TV, with a USB keyboard |
| How it is chosen | No keyboard attached at startup | A keyboard is attached at startup |
| HDMI output | 720x480, which the converter turns into the screen's 480i composite signal | The screen's own preferred mode (usually 1080p) |
| Home screen | Three-column layout sized for 480 lines | Six-column HD layout |

Car mode keeps HDMI rather than the Pi's composite pads because HDMI carries
the audio too; the Zero 2 W has no analog audio jack. In car mode mpv sets the
mode with `--drm-mode=720x480` and corrects for the 4:3 screen's non-square
pixels, so pictures are not stretched. Setup also adds
`video=HDMI-A-1:720x480@60` to `/boot/firmware/cmdline.txt` so boot messages
use the same mode and the converter never has to resync. In debug mode the
console still starts at 720x480 and mpv switches to the screen's preferred
mode when the player starts.

The mode is checked once, when the player starts. After plugging a keyboard in
or out, reboot or restart the player (`make pi-restart`) to switch. A TV
remote sending commands over HDMI does not count as a keyboard. To force a
mode regardless of the keyboard, set for example
`Environment=MAGICBOXIE_DISPLAY_MODE=1920x1080` in the player's unit (and
remove the `video=` entry from `cmdline.txt` for a screen that should never
use 480 lines).

## Add movies

Place supported video files in `/content`, then ask the daemon to rescan:

```sh
cp "My Movie.mp4" /content/
curl -X POST http://localhost:8000/api/rescan
```

The service generates thumbnails in the background while playback is idle.
The media server makes the 480p copy the player downloads into `/content`.
Movies that aren't that copy (originals, or files added by hand) are
transcoded on the player to the same format, one at a time, only while
nothing plays or downloads and the device is under 70°C. When the server's copy is newer than what the
player has (a movie downloaded as its full-size original, or a copy made
with an older encoding), the player downloads it again and swaps it in.

## Update the device

From the checkout on the Pi, pull the latest code, refresh dependencies and
the service definition, and restart:

```sh
cd ~/magicboxie-player
make pi
```

## Service commands

Run these from `~/magicboxie-player`:

```sh
make pi-start
make pi-stop
make pi-restart
make pi-logs
```

To rerun the complete installer safely, use the original bootstrap command.
It detects the existing checkout, updates it, and reapplies the installation.

## Troubleshooting

- Confirm the daemon is running with `systemctl status magicboxie-player`.
- Inspect recent logs with `journalctl -u magicboxie-player -n 100`.
- Without SSH (for example from a phone on the hotspot), open the device's web page, tap the gear, then **Logs**: it has Player, Wi-Fi and Updates tabs with this boot's logs (times in seconds since boot); the Wi-Fi tab also lists the saved network names, NetworkManager's Wi-Fi profiles, the adapter state and the networks in range.
- Check startup selection with `systemctl status magicboxie-wifi-startup`
  and `journalctl -u magicboxie-wifi-startup -b`.
- Check the hotspot with `systemctl status magicboxie-hotspot` and
  `journalctl -u magicboxie-hotspot -n 100`.
- Confirm the open AP with `nmcli connection show magicboxie-hotspot`.
- Confirm the portal with `curl http://10.42.0.1/`.
- Confirm Bluetooth is available with `bluetoothctl show`.
- Confirm the API locally with `curl http://localhost:8000/api/version`.
- Reboot after the initial installation if you plan to run the daemon manually;
  the installer changes the user's `video`, `input`, and `bluetooth` groups.

## Local development

With Docker installed, run:

```sh
make dev
```

This builds the development image, seeds sample movies when needed, and starts
the HTTP transport. Run the test suite with `make test`.

Startup and recovery:

- Startup lists movies without running ffprobe or ffmpeg. Durations are cached by file size and modification time; missing metadata and thumbnails are filled in after playback stops.
- Downloads, uploads, thumbnails, metadata, and playback state use temporary files and atomic replacement. Completed writes are synced to disk. Startup removes abandoned partial files.
- Playback failures quarantine the rejected file with a `.corrupt` suffix. A failed copy transcoded here by an older release falls back to the movie's own file; a failed movie file stops playback and returns to the idle screen. Quarantined files remain available for inspection.
- Each movie keeps its own resume position, checkpointed every five seconds and saved immediately on Stop and movie changes. Natural completion clears that movie's position and returns to the idle screen; nothing plays next automatically. Startup resumes only the saved movie, never a random one, and honors input received during startup.
- HTTP `update_status` and BLE characteristic `...000000000009` report a UTF-8 update message (empty BLE value / null HTTP field when inactive). Software updates wait for the current movie to finish and suppress startup resume until installation ends. The iOS app displays this over either transport.
- The updater records the successfully installed revision separately from Git HEAD, so an interrupted installation is retried. Its live status belongs to the updater process and expires rather than leaving a permanent updating indication after a crash.

Background scheduling:

- Player/HDMI initialization overlaps partial-file recovery and the fast library scan. Initial screen rendering runs alongside control-service startup.
- Screen rendering, durable playback saves, upload/download writes, metadata writes, and completed-file publication run in worker threads. Writes still finish durably before the corresponding operation succeeds; cancellation waits for an active disk write before cleanup.
- Home-server registration continues during playback. Downloads continue during playback but are capped at 1 MB/s to protect the Pi Zero's single core. Library scans of new files wait until playback stops. Priority: playback, downloads, slideshow.
- Every minute the device checks its temperature. At 80 °C or above a playing movie is paused and a "too hot" note shows on the web page and idle screen; the note clears below 70 °C. Playback is never resumed automatically.
- Pending background tasks are cancelled and joined on shutdown. A completed background screen render cannot replace a movie selected while it was rendering.
