"""The GATT service exposed to the iOS app: wires BLE characteristics to a
PlaybackController. Wire format only concerns itself here - actual movie
library/mpv logic lives in PlaybackController so it can be shared with the
HTTP transport too."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bluez_peripheral.gatt.characteristic import CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.characteristic import characteristic
from bluez_peripheral.gatt.service import Service

from ..controllers.playback_controller import PlaybackController
from ..models import protocol
from ..models.protocol import PlaybackState
from ..util import local_ip
from .wifi_provisioning import apply_wifi_credentials

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL_SECONDS = 1.0
# Position updates are coarse anyway; poll mpv half as often while it plays.
PLAYING_STATUS_POLL_INTERVAL_SECONDS = 2.0


class MagicBoxieService(Service):
    def __init__(self, controller: PlaybackController, http_port: int):
        super().__init__(protocol.SERVICE_UUID, True)
        self._controller = controller
        self._status_bytes = protocol.encode_status(PlaybackState.idle())
        self._http_port = http_port
        self._transcode_status_bytes = protocol.encode_transcode_status(None)
        self._poll_task: Optional[asyncio.Task] = None
        self._library_bytes = b""
        self._update_status_bytes = b""

    def start_status_polling(self) -> None:
        self._poll_task = asyncio.create_task(self._poll_status_loop())

    async def stop_status_polling(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()

    async def _poll_status_loop(self) -> None:
        while True:
            await asyncio.sleep(
                STATUS_POLL_INTERVAL_SECONDS if self._controller.is_idle else PLAYING_STATUS_POLL_INTERVAL_SECONDS)
            await self._refresh_status()
            self._refresh_transcode_status()
            self._refresh_update_status()

    async def _refresh_status(self) -> None:
        state = await self._controller.refresh_status()
        new_bytes = protocol.encode_status(state)
        if new_bytes != self._status_bytes:
            self._status_bytes = new_bytes
            self.status.changed(new_bytes)

    def _refresh_transcode_status(self) -> None:
        new_bytes = protocol.encode_transcode_status(self._controller.currently_transcoding_movie_id)
        if new_bytes != self._transcode_status_bytes:
            self._transcode_status_bytes = new_bytes
            self.transcode_status.changed(new_bytes)

    def _refresh_update_status(self) -> None:
        value = (self._controller.update_status or "").encode("utf-8")
        if value != self._update_status_bytes:
            self._update_status_bytes = value
            self.update_status.changed(value)

    @characteristic(protocol.UPDATE_STATUS_CHARACTERISTIC_UUID, CharFlags.READ | CharFlags.NOTIFY)
    def update_status(self, options):
        return (self._controller.update_status or "").encode("utf-8")[options.offset:]

    async def _handle_and_refresh(self, cmd: protocol.Command) -> None:
        await self._controller.handle_command(cmd)
        await self._refresh_status()

    # MARK: - Characteristics

    @characteristic(protocol.COMMAND_CHARACTERISTIC_UUID, CharFlags.WRITE)
    def command(self, options):
        pass  # write-only; value handled by the setter below

    @command.setter
    def _command_write(self, value, options):
        try:
            cmd = protocol.decode_command(bytes(value))
        except ValueError as exc:
            logger.warning("Dropping malformed command payload: %s", exc)
            return
        asyncio.create_task(self._handle_and_refresh(cmd))

    @characteristic(protocol.STATUS_CHARACTERISTIC_UUID, CharFlags.READ | CharFlags.NOTIFY)
    def status(self, options):
        return self._status_bytes

    @characteristic(protocol.LIBRARY_CHARACTERISTIC_UUID, CharFlags.READ)
    def library(self, options):
        # Computed fresh at the start of every *logical* read rather than
        # cached at construction time - the library now scans concurrently
        # with BLE startup (see _run_library_scan in main.py) instead of
        # before it, so a cached snapshot taken at construction would be
        # stuck empty forever once the scan finishes (no NOTIFY on this
        # characteristic to push an update, and nothing to have called it
        # anyway).
        #
        # "Start of every logical read", not "every call": a value this
        # long doesn't arrive in one shot - BlueZ splits it into multiple
        # ATT "Read Blob Request" calls here, one per options.offset, for
        # the client to reassemble (options.offset must be honored for
        # exactly this reason - returning the full value regardless of
        # offset made every continuation read repeat from byte 0,
        # corrupting the reassembly). Recomputing encode_library() fresh on
        # *each* of those fragment calls re-opened the same class of bug at
        # a smaller scale: the library can mutate between fragments (a
        # home-sync download landing mid-reassembly, observed on real
        # hardware), so two fragments of what's supposed to be one
        # contiguous string could get sliced from two different snapshots -
        # tearing the reassembled payload into something that fails to
        # parse into any movies at all, even though the library is
        # genuinely non-empty. offset == 0 always marks the start of a new
        # logical read, so snapshot only there and reuse it for every
        # subsequent fragment of that same read.
        if options.offset == 0:
            self._library_bytes = protocol.encode_library(self._controller.movies)
        return self._library_bytes[options.offset :]

    @characteristic(protocol.NETWORK_INFO_CHARACTERISTIC_UUID, CharFlags.READ)
    def network_info(self, options):
        # Computed fresh on every read, like library() above and for the
        # same reason: a value snapshotted once at construction time would
        # go stale the moment the device's DHCP-assigned LAN IP changes
        # without a full process restart (observed repeatedly on real
        # hardware - a WiFi drop/reconnect can renew the lease to a new
        # address while this daemon keeps running). A stale IP here
        # silently strands the iOS app's HTTP client on a dead address
        # forever, with BLE control still working fine - exactly the
        # "connected but no movies" failure mode this was traced to.
        url = f"http://{local_ip()}:{self._http_port}"
        return url.encode("utf-8")[options.offset :]

    @characteristic(protocol.TRANSCODE_STATUS_CHARACTERISTIC_UUID, CharFlags.READ | CharFlags.NOTIFY)
    def transcode_status(self, options):
        return self._transcode_status_bytes[options.offset :]

    @characteristic(protocol.API_VERSION_CHARACTERISTIC_UUID, CharFlags.READ)
    def api_version(self, options):
        # No NOTIFY - a running device's protocol version can't change
        # without a restart, so there's nothing to push an update for.
        return protocol.encode_api_version()[options.offset :]

    @characteristic(protocol.WIFI_PROVISION_CHARACTERISTIC_UUID, CharFlags.WRITE)
    def wifi_provision(self, options):
        pass  # write-only; value handled by the setter below

    @wifi_provision.setter
    def _wifi_provision_write(self, value, options):
        try:
            ssid, password = protocol.decode_wifi_credentials(bytes(value))
        except ValueError as exc:
            logger.warning("Dropping malformed WiFi provisioning payload: %s", exc)
            return
        # Fire-and-forget, same as command's own setter above: nmcli can
        # take several seconds (scanning, associating, DHCP), and this
        # setter itself must return immediately to let BlueZ send the ATT
        # write response - the phone finds out whether it worked by
        # watching wifiBaseURL/deviceIPAddress resolve to something on the
        # new network, not from this write itself.
        asyncio.create_task(apply_wifi_credentials(ssid, password))
