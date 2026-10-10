"""Entrypoint: wires the movie library, mpv, and the transports together.

Production ("ble") mode runs the BLE GATT service, the HTTP web service, and
an mDNS advertisement of that HTTP service concurrently: BLE is what the app
uses for control (and is a fallback for discovering the device's WiFi address
when mDNS multicast doesn't reach it), but BLE's tiny ATT payloads are a poor
fit for bulk data (movie library, thumbnails), so the app also discovers the
device directly over WiFi via mDNS and uses that for those. "http" mode
(dev/testing - no Bluetooth required, e.g. no BlueZ on Docker Desktop/macOS)
runs the HTTP service and mDNS advertisement alone, without BLE.

Movies are not transcoded on this device: the media server makes the 480p
copy this device downloads (see services/home_sync_service.py).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import List

from .controllers.playback_controller import PlaybackController
from .display import display_mode, is_standard_definition
from .models.library import MovieLibrary
from .views.player import MpvController
from .update_status import read_message, read_status
from .storage import run_io
from .util import host_resolves, local_ip, sleep_unless_stopped

logger = logging.getLogger(__name__)

DEVICE_NAME = "MagicBoxiePlayer"
MOVIES_DIR = Path(os.environ.get("MAGICBOXIE_MOVIES_DIR", "/movies"))
THUMBNAIL_DIR = Path(os.environ.get("MAGICBOXIE_THUMBNAIL_DIR", "/var/lib/magicboxie/thumbnails"))
TRANSCODE_DIR = Path(os.environ.get("MAGICBOXIE_TRANSCODE_DIR", "/var/lib/magicboxie/transcoded"))
# Where the currently-selected movie/position gets persisted so a power loss
# or reboot resumes instead of dropping back to the idle screen - see
# PlaybackController.restore_last_playback. Lives alongside the thumbnail
# cache rather than under MOVIES_DIR since that's typically mounted
# read-only (see library.py's own THUMBNAIL_DIR comment).
PLAYBACK_STATE_PATH = THUMBNAIL_DIR / "playback_state.json"

# "ble" (default, real device) or "http" (dev/testing - no Bluetooth required,
# e.g. when there's no BlueZ available such as Docker Desktop on macOS).
TRANSPORT = os.environ.get("MAGICBOXIE_TRANSPORT", "ble")
HTTP_PORT = int(os.environ.get("MAGICBOXIE_HTTP_PORT", "8000"))
# Pi installs enable port 80 for captive-portal probes; dev needs no privilege.
PORTAL_PORT = int(os.environ.get("MAGICBOXIE_PORTAL_PORT", "0"))

# bluez expires an advert after its Timeout elapses; re-registering periodically
# (well before that) keeps the device discoverable indefinitely.
ADVERT_TIMEOUT_SECONDS = 180
ADVERT_REFRESH_SECONDS = 150

# How long to wait before retrying BLE setup after it fails (e.g. BlueZ not
# having registered its adapter over D-Bus yet at boot).
BLE_RETRY_SECONDS = 5

# How long to wait before retrying mDNS registration after it fails (e.g. a
# stale record from a previous instance still cached on the network under
# the same name - see _run_mdns).
MDNS_RETRY_SECONDS = 5

# The home server (MagicBoxie-web) this device checks in with for new content
# when it has internet - see services/home_sync_service.py. Unset by default:
# the device works standalone (BLE/HTTP + whatever's already on disk), this
# is opportunistic on top of that, not a requirement.
HOME_SERVER_URL = os.environ.get("MAGICBOXIE_HOME_SERVER_URL", "")
HOME_SERVER_PASSWORD = os.environ.get("MAGICBOXIE_HOME_SERVER_PASSWORD", "")
# A minute, not longer: the device is meant to pick up newly-synced-enabled
# movies promptly whenever it happens to be on the home WiFi, and a failed
# check-in (server unreachable) is cheap and expected - see HomeServerSync.
HOME_SERVER_CHECKIN_SECONDS = int(os.environ.get("MAGICBOXIE_HOME_SERVER_CHECKIN_SECONDS", "60"))
# While the server still has movies for this device, check in this often.
HOME_SERVER_BUSY_RETRY_SECONDS = 10
# While the server's name does not resolve (not on the home network), look
# again this often, so a sync starts soon after joining that network.
HOME_SERVER_RESOLVE_RETRY_SECONDS = 15
# How long each movie stays up in the SD idle screen's slideshow.
SLIDESHOW_SECONDS = 8


def _mpv_output_args() -> List[str]:
    # Defaults target rendering straight to the framebuffer via DRM/KMS - the
    # Pi's own HDMI output, with no desktop environment running.
    # Override with MAGICBOXIE_MPV_ARGS if your hardware needs a different --vo/--gpu-context.
    # --drm-mode picks the HDMI mode mpv sets (see display.py: 720x480 by
    # default, the screen's preferred mode with a keyboard attached). 720x480 fills a 4:3 TV, so its pixels are 8:9 rather than
    # square; --monitorpixelaspect keeps pictures from looking stretched.
    mode = display_mode()
    default = "--vo=gpu --gpu-context=drm --drm-mode=" + (f"{mode[0]}x{mode[1]}" if mode else "preferred")
    if is_standard_definition():
        default += " --monitorpixelaspect=0.8889"
    raw = os.environ.get("MAGICBOXIE_MPV_ARGS", default)
    return raw.split()


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not MOVIES_DIR.is_dir():
        raise SystemExit(f"Movies directory does not exist: {MOVIES_DIR}")

    library = MovieLibrary(MOVIES_DIR, thumbnail_dir=THUMBNAIL_DIR, transcode_dir=TRANSCODE_DIR)

    player = MpvController(extra_args=_mpv_output_args())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    startup_decided = asyncio.Event()
    controller = None
    tasks = []
    workers_done = None
    stopped = None
    try:
        await _prepare_startup(library, player)
        controller = await run_io(PlaybackController, library, player, state_path=PLAYBACK_STATE_PATH)
        if stop_event.is_set():
            return
        workers = [
            _run_http(controller, stop_event),
            _run_keyboard(controller, stop_event),
            _run_status_message(controller, stop_event, startup_decided),
            _run_mdns(stop_event),
            _run_library_scan(controller, stop_event, startup_decided, prepared=True),
            _run_playback(controller, stop_event),
            _run_idle_dim(controller, stop_event),
            _run_thermal(controller, stop_event),
            _run_slideshow(controller, stop_event, startup_decided),
        ]
        if TRANSPORT != "http":
            workers.append(_run_ble(controller, stop_event))
        if HOME_SERVER_URL:
            workers.append(_run_home_sync(controller, stop_event))
        tasks = [asyncio.create_task(worker) for worker in workers]
        workers_done = asyncio.gather(*tasks)
        stopped = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait([workers_done, stopped], return_when=asyncio.FIRST_COMPLETED)
        if workers_done in done:
            workers_done.result()
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if workers_done is not None:
            await asyncio.gather(workers_done, return_exceptions=True)
        if stopped is not None:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
        if controller is not None:
            try:
                await asyncio.wait_for(controller.save_position_now(), timeout=2)
            except Exception:
                logger.warning("Could not save the playback position on exit")
        await player.stop_process()


async def _prepare_startup(library: MovieLibrary, player: MpvController) -> None:
    """Overlap mpv/HDMI setup with recovery and the lightweight library scan."""
    async def prepare_library():
        await run_io(library.cleanup_partial_files)
        await run_io(library.scan, fast=True)

    tasks = [asyncio.create_task(player.start()), asyncio.create_task(prepare_library())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_http(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    from aiohttp import web

    from web.web_service import create_app

    app = create_app(controller)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
        logger.info("Serving MagicBoxie web API on port %d", HTTP_PORT)
        if PORTAL_PORT and PORTAL_PORT != HTTP_PORT:
            try:
                await web.TCPSite(runner, "0.0.0.0", PORTAL_PORT).start()
                logger.info("Serving MagicBoxie captive portal on port %d", PORTAL_PORT)
            except OSError as exc:
                # Something else (e.g. nginx) holds the port: keep the API
                # on HTTP_PORT up rather than taking the whole server down.
                logger.warning("Captive portal unavailable on port %d (%s)", PORTAL_PORT, exc)
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def _run_mdns(stop_event: asyncio.Event) -> None:
    """Advertises the HTTP API over mDNS/Bonjour so the app can find this
    device on the local WiFi network without needing a BLE connection first.

    Retries on any failure (e.g. zeroconf.NonUniqueNameException, which a
    fast restart loop can trigger if a previous instance's record hasn't
    expired from the network's cache yet) rather than letting it propagate
    up to the `asyncio.gather` in `_run()` and take down BLE/HTTP with it -
    mirrors _run_ble's identical rationale for the same asyncio.gather.
    """
    from .services.mdns_service import MdnsAdvertiser

    while not stop_event.is_set():
        advertiser = MdnsAdvertiser(DEVICE_NAME, local_ip(), HTTP_PORT)
        try:
            await advertiser.start()
        except Exception:
            logger.exception("mDNS advertising failed - retrying in %ds", MDNS_RETRY_SECONDS)
            await sleep_unless_stopped(stop_event, MDNS_RETRY_SECONDS)
            continue

        try:
            await stop_event.wait()
        finally:
            await advertiser.stop()


async def _run_library_scan(controller: PlaybackController, stop_event: asyncio.Event,
                            startup_decided: asyncio.Event, *, prepared: bool = False) -> None:
    """Scan without blocking transports, then resume the last movie unless input arrived.

    The idle screen is not drawn at startup: rendering it competes for the
    weak CPU with the resume of the previous movie, which is what should
    reach the screen first. It is drawn only once resuming has been ruled out."""
    try:
        if not prepared:
            await run_io(controller.library.scan, fast=True)
        if stop_event.is_set():
            return
        if await run_io(read_status) is not None:
            await controller.show_idle_screen()
        while not stop_event.is_set() and await run_io(read_status) is not None:
            await sleep_unless_stopped(stop_event, 1)
        if not stop_event.is_set():
            await controller.resume_on_startup()
    finally:
        startup_decided.set()
    if controller.is_idle and not stop_event.is_set():
        await _show_idle_screen_logging_errors(controller)
    # Probes and frame grabs can wait until the user stops playback.
    while not stop_event.is_set():
        if controller.is_idle:
            await run_io(controller.library.scan)
            if controller.is_idle and not stop_event.is_set():
                await _show_idle_screen_logging_errors(controller)
            return
        await sleep_unless_stopped(stop_event, 5)


async def _show_idle_screen_logging_errors(controller: PlaybackController) -> None:
    """A slow mpv right after boot (its IPC reply times out) shouldn't take
    the whole daemon down with it; the slideshow timer redraws shortly."""
    try:
        await controller.show_idle_screen()
    except Exception:
        logger.exception("Idle screen refresh failed")


async def _run_playback(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Advance and persist playback even without connected clients or BLE."""
    while not stop_event.is_set():
        await controller.refresh_status()
        await sleep_unless_stopped(stop_event, 1)


async def _run_slideshow(controller: PlaybackController, stop_event: asyncio.Event,
                         startup_decided: asyncio.Event) -> None:
    """On the 720x480 TV output the idle screen shows one movie at a time;
    step through them while nothing is playing. HD keeps its static grid."""
    if not is_standard_definition():
        return
    await startup_decided.wait()
    while not stop_event.is_set():
        await sleep_unless_stopped(stop_event, SLIDESHOW_SECONDS)
        if controller.is_idle and not stop_event.is_set():
            await controller.advance_slideshow()


async def _run_idle_dim(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Dims the idle screen after extended inactivity - see
    services/idle_dim_service.py."""
    from .services.idle_dim_service import IdleDimService

    await IdleDimService(controller).run(stop_event)


async def _run_thermal(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Pauses playback when the device overheats - see services/thermal_service.py."""
    from .services.thermal_service import ThermalService

    await ThermalService(controller).run(stop_event)


async def _run_status_message(controller: PlaybackController, stop_event: asyncio.Event,
                              startup_decided: asyncio.Event) -> None:
    """Shows what the device is doing (internet/update progress from the
    boot-update and self-update processes, downloads, transcodes) as a big
    banner on the idle screen."""
    shown = None
    while not stop_event.is_set():
        controller.status_message = await run_io(read_message)
        message = controller.activity_message
        # Hold off drawing until startup has decided whether to resume a movie.
        if message != shown and (startup_decided.is_set() or not controller.is_idle):
            shown = message
            if controller.is_idle:
                await controller.show_idle_screen()
        # Nothing to show over a playing movie, so check rarely then.
        await sleep_unless_stopped(stop_event, 2 if controller.is_idle else 10)


async def _run_keyboard(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Escape-to-quit from a directly-attached USB keyboard - the device's
    only local input, independent of BLE/HTTP and the iOS app. Runs in both
    transport modes, and is a no-op if no keyboard is ever attached."""
    from .services.keyboard_service import KeyboardService

    await KeyboardService(controller).run(stop_event)


async def _run_ble(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Retries the whole BLE setup/advertise cycle on any failure, rather than
    letting one propagate up to the `asyncio.gather` in `_run()` and take
    down HTTP/mDNS with it - BLE is meant to be a resilient fallback control
    path, not a single-shot one, so a D-Bus hiccup or bluetoothd restarting
    should self-heal rather than take the whole daemon down with it.
    """
    while not stop_event.is_set():
        try:
            await _run_ble_once(controller, stop_event)
        except Exception:
            logger.exception("BLE service failed - retrying in %ds", BLE_RETRY_SECONDS)
        await sleep_unless_stopped(stop_event, BLE_RETRY_SECONDS)


async def _first_bluez_adapter(bus):
    """bluez_peripheral.util.Adapter.get_first()/get_all() assume every child
    node under /org/bluez is an adapter and blow up otherwise - but BlueZ
    also exposes non-adapter objects there on stock installs (e.g.
    /org/bluez/test, a built-in org.bluez.SimAccessTest1 debug object), which
    makes the upstream helper unusable on a real system. Do the same
    lookup ourselves, filtering to nodes that actually implement
    org.bluez.Adapter1.
    """
    from bluez_peripheral.util import Adapter

    root = await bus.introspect("org.bluez", "/org/bluez")
    for node in root.nodes:
        path = "/org/bluez/" + node.name
        introspection = await bus.introspect("org.bluez", path)
        if not any(interface.name == "org.bluez.Adapter1" for interface in introspection.interfaces):
            continue
        proxy = bus.get_proxy_object("org.bluez", path, introspection)
        return Adapter(proxy)
    raise RuntimeError("No Bluetooth adapter (org.bluez.Adapter1) found under /org/bluez")


# bluez_peripheral.advert.Advertisement.register() always exports itself at
# this fixed default path (it doesn't expose a way to override it from the
# call sites we use), and only unexports the local D-Bus object afterwards -
# it never tells BlueZ to unregister the advertisement. See
# _unregister_advertisement below.
_ADVERTISEMENT_PATH = "/com/spacecheese/bluez_peripheral/advert0"


async def _unregister_advertisement(adapter) -> None:
    """Tells BlueZ to drop whatever advertisement is currently registered at
    Advertisement's fixed default path, if any. Needed before every
    (re-)registration: bluez_peripheral's Advertisement.register() (see
    _ADVERTISEMENT_PATH) never does this itself, so registering a second
    Advertisement at the same path - which our periodic refresh loop below
    does every ADVERT_REFRESH_SECONDS - gets rejected by BlueZ with
    'Already Exists', permanently killing the advertisement after the first
    refresh. A no-op (DBusError swallowed) the first time through, when
    nothing's registered yet.
    """
    from dbus_next.errors import DBusError

    interface = adapter._proxy.get_interface("org.bluez.LEAdvertisingManager1")
    try:
        await interface.call_unregister_advertisement(_ADVERTISEMENT_PATH)
    except DBusError:
        pass


async def _run_ble_once(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    from bluez_peripheral.advert import Advertisement
    from bluez_peripheral.agent import NoIoAgent
    from bluez_peripheral.util import get_message_bus

    from .models import protocol
    from .services.ble_service import MagicBoxieService

    service = MagicBoxieService(controller, HTTP_PORT)

    bus = await get_message_bus()

    # Resolved ourselves (see _first_bluez_adapter) and passed in explicitly -
    # service.register()'s own default (adapter=None) falls back to bluez_peripheral's
    # buggy Adapter.get_first(), which would blow up here exactly as it does
    # for us below without this.
    adapter = await _first_bluez_adapter(bus)
    await service.register(bus, adapter=adapter)

    # Required for bluez to complete pairing requests; "no IO" since this
    # device has no screen/keyboard of its own to confirm a passkey with.
    agent = NoIoAgent()
    await agent.register(bus)

    await adapter.set_alias(DEVICE_NAME)

    service.start_status_polling()

    try:
        while not stop_event.is_set():
            # No local name here: legacy BLE advertising packets cap out at
            # 31 bytes, and our 128-bit custom service UUID alone already
            # takes 18 of those (plus 3 for the flags BlueZ adds
            # automatically) - there's no room left for a readable name too,
            # and bluez_peripheral's LocalName property is sent unconditionally
            # once set, so registration fails outright rather than silently
            # dropping it. Not a problem for the app: it scans by service
            # UUID (see MediaControlProtocol.serviceUUID), never by name.
            # adapter.set_alias() above still gives it a friendly name for
            # anything that connects and reads the Generic Access Profile.
            await _unregister_advertisement(adapter)
            advert = Advertisement("", [protocol.SERVICE_UUID], 0x0000, ADVERT_TIMEOUT_SECONDS)
            await advert.register(bus, adapter)
            logger.info("Advertising %r (WiFi: http://%s:%d)", DEVICE_NAME, local_ip(), HTTP_PORT)
            await sleep_unless_stopped(stop_event, ADVERT_REFRESH_SECONDS)
    finally:
        await service.stop_status_polling()


async def _run_home_sync(controller: PlaybackController, stop_event: asyncio.Event) -> None:
    """Retries a home server check-in on an interval. The device is offline
    most of the time, so a failed attempt (no route, DNS failure, etc.) is
    expected and just gets logged - not treated as fatal."""
    from .services.home_sync_service import HomeServerSync

    redraw_requested = asyncio.Event()

    async def redraw_idle_screen():
        while True:
            await redraw_requested.wait()
            redraw_requested.clear()
            try:
                await controller.show_idle_screen()
            except Exception:
                logger.exception("Idle screen refresh failed")

    def set_syncing_title(title: str | None) -> None:
        controller.currently_syncing_movie_title = title
        if controller.is_idle:
            redraw_requested.set()

    sync = HomeServerSync(
        controller.library,
        HOME_SERVER_URL,
        HOME_SERVER_PASSWORD,
        on_progress=set_syncing_title,
        is_idle=lambda: controller.is_idle,
        on_busy=lambda busy: setattr(controller, "sync_busy", busy),
    )
    controller.sync_activity = sync.activity
    redraw_task = asyncio.create_task(redraw_idle_screen())
    try:
        while not stop_event.is_set():
            if not await host_resolves(HOME_SERVER_URL):
                sync.activity.reachable = False
                await sleep_unless_stopped(stop_event, HOME_SERVER_RESOLVE_RETRY_SECONDS)
                continue
            try:
                await sync.check_in()
            except Exception:
                logger.exception("Home server check-in failed unexpectedly")
            # Still movies to fetch (a download failed, or the server is
            # still making 480p copies)? Look again soon, not in a minute.
            busy = sync.activity.reachable and (sync.activity.queued or sync.activity.preparing)
            await sleep_unless_stopped(stop_event, HOME_SERVER_BUSY_RETRY_SECONDS if busy else HOME_SERVER_CHECKIN_SECONDS)
    finally:
        redraw_task.cancel()
        await asyncio.gather(redraw_task, return_exceptions=True)


def run() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    run()
