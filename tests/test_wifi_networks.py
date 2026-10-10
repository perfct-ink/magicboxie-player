import json
import stat
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from player_app.wifi_networks import load_networks, save_network
from player_app import wifi_startup


def test_credentials_round_trip_update_and_private_permissions(tmp_path):
    path = tmp_path / "credentials" / "wifi-networks.json"
    save_network("Mitera", "first-password", path)
    save_network("AV-iPhone17Pro", "phone-password", path)
    save_network("Mitera", "changed-password", path)
    assert load_networks(path) == [
        {"ssid": "AV-iPhone17Pro", "password": "phone-password"},
        {"ssid": "Mitera", "password": "changed-password"},
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_concurrent_saves_preserve_every_network(tmp_path):
    path = tmp_path / "wifi-networks.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: save_network(f"WiFi-{n}", "password", path), range(10)))
    assert len(load_networks(path)) == 10


def test_invalid_file_is_preserved_on_provisioning(tmp_path):
    path = tmp_path / "wifi-networks.json"
    path.write_text('{"version": 1, "networks": "invalid"}')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_network("Mitera", "password", path)
    assert path.read_bytes() == before


def test_invalid_entry_does_not_replace_saved_credentials(tmp_path):
    path = tmp_path / "wifi-networks.json"
    save_network("Mitera", "password", path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_network("x" * 33, "password", path)
    assert path.read_bytes() == before


def test_startup_restores_file_without_duplicate_profiles():
    networks = [{"ssid": "Mitera", "password": "example-password"}]
    with patch.object(wifi_startup, "load_networks", return_value=networks), \
            patch.object(wifi_startup, "nmcli", side_effect=["", "created", "", "uuid", "updated", ""]) as cli:
        wifi_startup.restore_saved_networks()
        wifi_startup.restore_saved_networks()
    assert cli.call_args_list[1].args[:3] == ("connection", "add", "type")
    assert cli.call_args_list[4].args[:3] == ("connection", "modify", "id")
    assert cli.call_args_list[1].args[-1] == "example-password"


def test_bad_credentials_file_keeps_startup_functional_and_logs_no_secrets(caplog):
    with patch.object(wifi_startup, "load_networks", side_effect=ValueError("secret-password")), \
            patch.object(wifi_startup, "nmcli") as cli:
        wifi_startup.restore_saved_networks()
    cli.assert_not_called()
    assert "secret-password" not in caplog.text


def test_add_missing_networks_keeps_existing_entries(tmp_path):
    from player_app.wifi_networks import add_missing_networks, load_networks

    seed = tmp_path / "seed.json"
    seed.write_text('{"version": 1, "networks": [{"ssid": "Mitera", "password": "seed"}, {"ssid": "Phone", "password": "p"}]}')
    device = tmp_path / "device.json"
    device.write_text('{"version": 1, "networks": [{"ssid": "Mitera", "password": "changed"}]}')
    assert add_missing_networks(seed, device) == ["Phone"]
    assert load_networks(device) == [{"ssid": "Mitera", "password": "changed"}, {"ssid": "Phone", "password": "p"}]
    assert add_missing_networks(seed, device) == []


def test_add_missing_networks_fills_an_empty_device_file(tmp_path):
    from player_app.wifi_networks import add_missing_networks, load_networks

    seed = tmp_path / "seed.json"
    seed.write_text('{"version": 1, "networks": [{"ssid": "Mitera", "password": "x"}]}')
    device = tmp_path / "device.json"
    device.write_text('{"version": 1, "networks": []}')
    assert add_missing_networks(seed, device) == ["Mitera"]
    assert [n["ssid"] for n in load_networks(device)] == ["Mitera"]


def test_removed_network_stays_removed_across_seed_deploys_and_save_brings_it_back(tmp_path):
    from player_app.wifi_networks import add_missing_networks, load_forgotten, remove_network

    seed = tmp_path / "seed.json"
    seed.write_text('{"version": 1, "networks": [{"ssid": "Mitera", "password": "seed"}, {"ssid": "Phone", "password": "p"}]}')
    device = tmp_path / "device.json"
    add_missing_networks(seed, device)
    assert remove_network("Phone", device) is True
    assert remove_network("Phone", device) is False
    assert load_networks(device) == [{"ssid": "Mitera", "password": "seed"}]
    assert add_missing_networks(seed, device) == []
    save_network("Phone", "new", device)
    assert load_forgotten(device) == []
    assert load_networks(device)[-1] == {"ssid": "Phone", "password": "new"}


def test_startup_deletes_profiles_of_removed_networks():
    keep = wifi_startup.profile_name("Mitera")
    stale = wifi_startup.profile_name("Old phone")
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("--get-values", "NAME"):
            return "\n".join([keep, stale, "magicboxie-hotspot", "Wired connection 1"])
        return "uuid" if args[0] == "--get-values" else ""

    with patch.object(wifi_startup, "load_networks", return_value=[{"ssid": "Mitera", "password": "x"}]), \
            patch.object(wifi_startup, "nmcli", side_effect=fake):
        wifi_startup.restore_saved_networks()
    deletes = [c for c in calls if c[:2] == ("connection", "delete")]
    assert deletes == [("connection", "delete", "id", stale)]
