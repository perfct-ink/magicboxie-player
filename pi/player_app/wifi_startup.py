"""Give NetworkManager 30 seconds to join saved Wi-Fi before starting the AP.

With --retry (run periodically by magicboxie-wifi-retry.timer), a device
stuck on its fallback hotspot with nobody connected to it drops the hotspot
for one more 30-second saved-Wi-Fi window, so it returns to a saved network
such as Mitera once it is back in range instead of waiting for a reboot.

Installed as a root-owned standalone script; only Python's standard library
is needed. NetworkManager scans and authenticates using its saved profiles.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import time

if __package__:
    from .wifi_networks import load_networks
else:
    from wifi_networks import load_networks

logger = logging.getLogger(__name__)
INTERFACE = "wlan0"
STARTUP_WAIT_SECONDS = 30


# Creating or changing a profile can take NetworkManager several seconds on
# a busy Zero at boot; polling queries stay short.
PROFILE_TIMEOUT_SECONDS = 15


def nmcli(*args: str, timeout: float = 2, log_failure: bool = False) -> str:
    try:
        result = subprocess.run(
            ["nmcli", "--terse", "--escape", "no", *args],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        if log_failure:
            logger.warning("nmcli %s failed: %s", " ".join(args[:3]), (exc.stderr or "").strip())
        return ""
    except (subprocess.SubprocessError, OSError) as exc:
        if log_failure:
            logger.warning("nmcli %s failed: %s", " ".join(args[:3]), exc)
        return ""


def connection_mode(timeout: float = 2) -> str:
    """Only an activated Wi-Fi client counts, not Ethernet or internet reachability."""
    fields = nmcli("--get-values", "GENERAL.STATE,GENERAL.CON-UUID",
                   "device", "show", INTERFACE, timeout=timeout).splitlines()
    if len(fields) != 2 or fields[0].split()[0] != "100" or not fields[1]:
        return ""
    mode = nmcli("--get-values", "802-11-wireless.mode", "connection", "show",
                 "uuid", fields[1], timeout=timeout)
    return mode


def wait_for_saved_wifi(wait_seconds: float = STARTUP_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Bound both queries to the remaining startup window.
        mode = connection_mode(timeout=min(2, remaining / 2))
        if mode == "infrastructure":
            logger.info("Connected to saved Wi-Fi; keeping hotspot off")
            return True
        if mode == "ap":
            # Re-running deployment must not disconnect existing AP clients.
            logger.info("Hotspot already active; keeping it running")
            return False
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def profile_name(ssid: str) -> str:
    return "magicboxie-saved-" + hashlib.sha256(ssid.encode()).hexdigest()[:16]


def restore_saved_networks() -> None:
    try:
        networks = load_networks()
    except (OSError, ValueError):
        logger.error("Cannot read Wi-Fi credentials file; keeping existing NetworkManager profiles")
        return
    for network in networks:
        ssid, password = network["ssid"], network["password"]
        # Stable names avoid creating a new connection on every boot.
        name = profile_name(ssid)
        exists = nmcli("--get-values", "connection.uuid", "connection", "show", "id", name,
                       timeout=PROFILE_TIMEOUT_SECONDS)
        settings = ["connection.autoconnect", "yes", "802-11-wireless.ssid", ssid,
                    "802-11-wireless.mode", "infrastructure", "ipv4.method", "auto"]
        if password:
            settings += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        elif exists:
            # Clear security if a previously protected SSID is now open.
            settings += ["802-11-wireless-security", ""]
        if exists:
            nmcli("connection", "modify", "id", name, *settings,
                  timeout=PROFILE_TIMEOUT_SECONDS, log_failure=True)
        else:
            nmcli("connection", "add", "type", "wifi", "ifname", INTERFACE,
                  "con-name", name, *settings, timeout=PROFILE_TIMEOUT_SECONDS, log_failure=True)
    # Drop profiles of networks removed from the settings page.
    wanted = {profile_name(network["ssid"]) for network in networks}
    for name in nmcli("--get-values", "NAME", "connection", "show", timeout=PROFILE_TIMEOUT_SECONDS).splitlines():
        if name.startswith("magicboxie-saved-") and name not in wanted:
            nmcli("connection", "delete", "id", name, timeout=PROFILE_TIMEOUT_SECONDS, log_failure=True)
            logger.info("Removed Wi-Fi profile %s (network no longer saved)", name)
    logger.info("Restored %d saved Wi-Fi network(s): %s", len(networks),
                ", ".join(network["ssid"] for network in networks) or "none")


def request_self_update() -> None:
    # Queue the existing updater instead of waiting for download/playback idle.
    # Starting an already running unit also coalesces timer/startup requests.
    logger.info("Saved Wi-Fi connected; requesting a self-update")
    try:
        subprocess.run(
            ["systemctl", "--no-block", "start", "magicboxie-self-update.service"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("Could not request self-update; keeping saved Wi-Fi connected")


def hotspot_has_clients() -> bool:
    """Anyone joined to the hotspot (e.g. on the setup page) keeps it up.
    If iw cannot answer, assume someone is there rather than cut them off."""
    try:
        result = subprocess.run(
            ["iw", "dev", INTERFACE, "station", "dump"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("Cannot list hotspot clients; keeping the hotspot up")
        return True
    return "Station" in result.stdout


def start_hotspot() -> None:
    logger.info("Starting MagicBoxie Player hotspot")
    subprocess.run(["systemctl", "start", "magicboxie-hotspot.service"], check=True)


def finish_search() -> None:
    if wait_for_saved_wifi():
        request_self_update()
    else:
        start_hotspot()


def retry_saved_wifi() -> None:
    if connection_mode() != "ap":
        return
    if hotspot_has_clients():
        logger.info("Hotspot has clients; not retrying saved Wi-Fi now")
        return
    logger.info("Hotspot is idle; retrying saved Wi-Fi for up to %d seconds", STARTUP_WAIT_SECONDS)
    subprocess.run(["systemctl", "stop", "magicboxie-hotspot.service"], check=False)
    nmcli("connection", "down", "id", "magicboxie-hotspot", timeout=15)
    finish_search()


def search() -> None:
    """Settings-menu "find networks": the single radio cannot broadcast and
    scan at once, so drop the hotspot, give saved Wi-Fi 30 seconds, then
    broadcast again if nothing connected."""
    restore_saved_networks()
    if connection_mode() == "ap":
        logger.info("Stopping hotspot to search for saved Wi-Fi")
        subprocess.run(["systemctl", "stop", "magicboxie-hotspot.service"], check=False)
        nmcli("connection", "down", "id", "magicboxie-hotspot", timeout=15)
    nmcli("radio", "wifi", "on")
    nmcli("device", "wifi", "rescan", "ifname", INTERFACE, timeout=10)
    finish_search()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--retry" in sys.argv[1:]:
        retry_saved_wifi()
        return
    if "--search" in sys.argv[1:]:
        search()
        return
    restore_saved_networks()
    nmcli("radio", "wifi", "on")
    nmcli("device", "set", INTERFACE, "autoconnect", "yes")
    logger.info("Waiting up to %d seconds for saved Wi-Fi on %s", STARTUP_WAIT_SECONDS, INTERFACE)
    if wait_for_saved_wifi():
        request_self_update()
    else:
        start_hotspot()


if __name__ == "__main__":
    main()
