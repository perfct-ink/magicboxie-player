"""Persistent Wi-Fi credentials, deliberately outside the Git checkout."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

if __package__:
    from .storage import atomic_write
else:  # Root-owned standalone startup scripts installed on the Pi.
    from storage import atomic_write

WIFI_NETWORKS_PATH = Path(os.environ.get("MAGICBOXIE_WIFI_NETWORKS_FILE", "/var/lib/magicboxie/wifi-networks.json"))


def load_networks(path: Path = WIFI_NETWORKS_PATH) -> list:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("networks"), list):
        raise ValueError("Invalid Wi-Fi credentials file format")
    networks = data["networks"]
    seen = set()
    for network in networks:
        if not isinstance(network, dict):
            raise ValueError("Invalid Wi-Fi entry")
        ssid, password = network.get("ssid"), network.get("password")
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or "\x00" in ssid:
            raise ValueError("Invalid Wi-Fi SSID")
        if not isinstance(password, str) or "\x00" in password:
            raise ValueError("Invalid Wi-Fi password")
        if ssid in seen:
            raise ValueError("Duplicate Wi-Fi SSID")
        seen.add(ssid)
    return networks


def load_forgotten(path: Path = WIFI_NETWORKS_PATH) -> list:
    """SSIDs removed from the settings page, so a deploy's seed file does not
    bring them back."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    forgotten = data.get("forgotten", []) if isinstance(data, dict) else []
    return [ssid for ssid in forgotten if isinstance(ssid, str)] if isinstance(forgotten, list) else []


def _write(path: Path, networks: list, forgotten: list) -> None:
    data = {"version": 1, "networks": networks}
    if forgotten:
        data["forgotten"] = forgotten
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode())


def save_network(ssid: str, password: str, path: Path = WIFI_NETWORKS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        networks = load_networks(path)
        networks = [entry for entry in networks if entry["ssid"] != ssid]
        networks.append({"ssid": ssid, "password": password})
        # Validate before replacing a complete file, including new entries.
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or "\x00" in ssid:
            raise ValueError("Invalid Wi-Fi SSID")
        if not isinstance(password, str) or "\x00" in password:
            raise ValueError("Invalid Wi-Fi password")
        _write(path, networks, [name for name in load_forgotten(path) if name != ssid])


def remove_network(ssid: str, path: Path = WIFI_NETWORKS_PATH) -> bool:
    """Forgets a saved network; returns False if it was not saved. Its
    NetworkManager profile goes at the next startup or Wi-Fi search."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        networks = load_networks(path)
        kept = [entry for entry in networks if entry["ssid"] != ssid]
        if len(kept) == len(networks):
            return False
        forgotten = [name for name in load_forgotten(path) if name != ssid] + [ssid]
        _write(path, kept, forgotten)
        return True


def add_missing_networks(seed_path: Path, path: Path = WIFI_NETWORKS_PATH) -> list:
    """Adds seed networks whose SSID the device file lacks (unless removed
    from the settings page), keeping every
    existing entry (and its password) as is. Lets a deploy deliver networks
    added to the tracked seed after the device was first installed."""
    seed = load_networks(seed_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        networks = load_networks(path)
        forgotten = load_forgotten(path)
        known = {entry["ssid"] for entry in networks} | set(forgotten)
        added = [entry for entry in seed if entry["ssid"] not in known]
        if added:
            _write(path, networks + added, forgotten)
        return [entry["ssid"] for entry in added]


if __name__ == "__main__":
    for name in add_missing_networks(Path(sys.argv[1])):
        print(f"Added saved Wi-Fi network {name!r}")
