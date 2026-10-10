"""Plain HTTP transport for dev/testing without Bluetooth.

Enable with MAGICBOXIE_TRANSPORT=http (see main.py). Mirrors the same
PlaybackController the BLE GATT service uses, just over JSON/REST instead of
GATT characteristics - useful when there's no BlueZ available (e.g. running
in Docker Desktop on macOS) or no BLE-capable client at hand.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from . import system_info
from .portal import PAGE, STATIC_DIR, WELCOME_PAGE, WELCOME_URL
from player_app.storage import publish_file, run_io
from player_app.wifi_networks import load_networks, remove_network, save_network
from player_app.controllers.playback_controller import PlaybackController
from player_app.models.library import VIDEO_EXTENSIONS
from player_app.models.protocol import API_VERSION, Command, Movie, Opcode
from player_app.util import cpu_temperature_celsius, get_throttle_status, internet_reachable, local_ip

_METADATA_FIELDS = ("title", "description", "year", "duration_seconds")

logger = logging.getLogger(__name__)

_OPCODE_BY_NAME = {opcode.name.lower(): opcode for opcode in Opcode}
_CONTROLLER_KEY = web.AppKey("controller", PlaybackController)
# Phones (by address) that tapped Done on the welcome page. Their OS probe
# URLs then get the "internet works" answer, so the sign-in sheet closes by
# itself. In memory only: a restart just shows the sheet again.
_RELEASED_KEY = web.AppKey("released", dict)
_RELEASE_SECONDS = 24 * 3600
_MAX_RELEASED = 256

# What each OS expects from its connectivity probe when there is "no captive
# portal": Apple a "Success" page, Android/Chrome an empty 204, Windows a
# fixed string.
# How often the web page should ask for status: the device tells it, so the
# page backs off while a movie plays (playback needs every cycle it can get).
_POLL_SECONDS_IDLE = 3
_POLL_SECONDS_PLAYING = 6


def _poll_seconds(controller: PlaybackController, playing: bool) -> int:
    return _POLL_SECONDS_PLAYING if playing else _POLL_SECONDS_IDLE


_PROBE_RESPONSES = {
    "/hotspot-detect.html": ("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>", 200, "text/html"),
    "/library/test/success.html": ("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>", 200, "text/html"),
    "/generate_204": ("", 204, "text/plain"),
    "/gen_204": ("", 204, "text/plain"),
    "/connecttest.txt": ("Microsoft Connect Test", 200, "text/plain"),
    "/ncsi.txt": ("Microsoft NCSI", 200, "text/plain"),
    "/success.txt": ("success\n", 200, "text/plain"),
}

# Generous but bounded - movie files are large, but this still guards against
# a truly unbounded upload filling the disk.
_MAX_UPLOAD_BYTES = 1024 ** 3 * 20  # 20GB

_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1MB


def create_app(controller: PlaybackController) -> web.Application:
    app = web.Application(client_max_size=_MAX_UPLOAD_BYTES)
    app[_CONTROLLER_KEY] = controller
    app[_RELEASED_KEY] = {}

    app.router.add_get("/", _get_portal)
    app.router.add_get("/welcome", _get_welcome)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_response_prepare.append(_revalidate_static)
    app.router.add_get("/api/movies", _get_movies)
    app.router.add_post("/api/movies", _post_movie)
    app.router.add_delete("/api/movies/{id}", _delete_movie)
    app.router.add_post("/api/rescan", _post_rescan)
    app.router.add_get("/api/movies/{id}/thumbnail", _get_thumbnail)
    app.router.add_post("/api/movies/{id}/thumbnail", _post_thumbnail)
    app.router.add_post("/api/movies/{id}/metadata", _post_metadata)
    app.router.add_get("/api/status", _get_status)
    app.router.add_get("/api/activity", _get_activity)
    app.router.add_post("/api/command", _post_command)
    app.router.add_get("/api/version", _get_version)
    app.router.add_get("/api/info", _get_info)
    app.router.add_get("/api/logs", _get_logs)
    app.router.add_post("/api/reboot", _post_reboot)
    app.router.add_post("/api/update", _post_update)
    app.router.add_post("/api/wifi/search", _post_wifi_search)
    app.router.add_get("/api/wifi/networks", _get_wifi_networks)
    app.router.add_post("/api/wifi/networks", _post_wifi_network)
    app.router.add_delete("/api/wifi/networks", _delete_wifi_network)
    app.router.add_get("/api/network", _get_network)
    app.router.add_post("/api/shutdown", _post_shutdown)
    app.router.add_post("/api/portal/done", _post_portal_done)
    # Android, Apple, and Windows probe different HTTP paths. An unexpected
    # HTML redirect (rather than their expected success response) opens login.
    app.router.add_get("/{path:.*}", _redirect_to_portal)
    return app


async def _revalidate_static(request: web.Request, response: web.StreamResponse) -> None:
    # Files are small and local; revalidate (ETag) so an updated device never
    # serves a phone stale JS/CSS.
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


async def _get_portal(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


async def _get_welcome(request: web.Request) -> web.Response:
    return web.Response(text=WELCOME_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


def _is_released(request: web.Request) -> bool:
    released = request.app[_RELEASED_KEY]
    since = released.get(request.remote)
    if since is None:
        return False
    if time.monotonic() - since > _RELEASE_SECONDS:
        del released[request.remote]
        return False
    return True


async def _post_portal_done(request: web.Request) -> web.Response:
    """The phone's user tapped Done on the welcome page: stop showing them
    the sign-in sheet (see _PROBE_RESPONSES)."""
    released = request.app[_RELEASED_KEY]
    if len(released) >= _MAX_RELEASED:
        del released[min(released, key=released.get)]
    released[request.remote] = time.monotonic()
    return web.json_response({"ok": True})


async def _redirect_to_portal(request: web.Request) -> web.Response:
    if request.path.startswith("/api/"):
        raise web.HTTPNotFound()
    probe = _PROBE_RESPONSES.get(request.path)
    if probe and _is_released(request):
        body, status, content_type = probe
        return web.Response(text=body, status=status, content_type=content_type, headers={"Cache-Control": "no-store"})
    raise web.HTTPFound(WELCOME_URL, headers={"Cache-Control": "no-store"})


def _movie_payload(controller: PlaybackController, movie: Movie) -> dict:
    """Base fields from the filesystem scan, overlaid with any
    phone-supplied metadata (title/description/year/duration) fetched
    online - see _post_metadata."""
    payload = {
        "id": movie.id,
        "title": movie.title,
        "duration_seconds": movie.duration_seconds,
        "description": None,
        "year": None,
        # Nothing transcodes on the device any more (the media server makes
        # the 480p copy); the field stays so existing apps keep working.
        "needs_transcoding": False,
        # Where playback would resume (0 = never played, or played to the
        # end) - the page draws a progress bar under movies watched partway.
        "position_seconds": controller.saved_position(movie.id),
    }
    payload.update(controller.library.metadata_for(movie.id))
    return payload


def _movies_payload(controller: PlaybackController) -> list:
    return [_movie_payload(controller, movie) for movie in controller.movies]


async def _get_movies(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER_KEY]
    return web.json_response(_movies_payload(controller))


async def _post_rescan(request: web.Request) -> web.Response:
    """Re-scans the movies directory - probing durations and generating any
    thumbnails that don't already exist - so files dropped onto the
    filesystem directly (outside POST /api/movies) show up without
    restarting the daemon."""
    controller = request.app[_CONTROLLER_KEY]
    await run_io(controller.library.scan)
    return web.json_response(_movies_payload(controller))


async def _delete_movie(request: web.Request) -> web.Response:
    """Permanently removes a movie from the device (see
    MovieLibrary.delete) - stops playback first if it's the one currently
    selected, so its file isn't yanked out from under mpv mid-playback."""
    controller = request.app[_CONTROLLER_KEY]
    try:
        movie_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid movie id"}, status=400)

    if not any(movie.id == movie_id for movie in controller.movies):
        return web.json_response({"error": "unknown movie id"}, status=404)

    state = await controller.refresh_status()
    if state.movie_id == movie_id:
        await controller.stop_and_show_idle_screen()

    await run_io(controller.library.delete, movie_id)
    return web.json_response({"ok": True})


async def _post_movie(request: web.Request) -> web.Response:
    """Accepts a whole movie file (e.g. shared into the iOS app from Photos/
    Dropbox/Files) and adds it to the library. Streams the body straight to
    disk in chunks rather than buffering it all in memory - these are large
    files and this runs on a Pi. The phone is expected to check GET /movies
    first and only call this when the title isn't already present; this
    endpoint itself just rejects an exact filename collision as a safety net.
    """
    controller = request.app[_CONTROLLER_KEY]
    filename = request.headers.get("X-Filename")
    if filename and request.headers.get("X-Filename-Encoding") == "uri":
        # Browsers can only send ISO-8859-1 header values, so the web page
        # percent-encodes names that contain other characters.
        filename = unquote(filename)
    if not filename or "/" in filename or filename.startswith("."):
        return web.json_response({"error": "missing or invalid X-Filename header"}, status=400)

    extension = Path(filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        return web.json_response({"error": f"unsupported file extension {extension!r}"}, status=400)

    dest_path = controller.library.root / filename
    if dest_path.exists():
        return web.json_response({"error": "a file with this name already exists"}, status=409)

    temporary = dest_path.with_name("." + dest_path.name + "." + uuid.uuid4().hex + ".partial")
    bytes_written = 0
    try:
        with temporary.open("xb") as f:
            async for chunk in request.content.iter_chunked(_UPLOAD_CHUNK_BYTES):
                await run_io(f.write, chunk)
                bytes_written += len(chunk)
        if bytes_written == 0:
            return web.json_response({"error": "empty body"}, status=400)
        await run_io(publish_file, temporary, dest_path)
    except FileExistsError:
        return web.json_response({"error": "upload already in progress"}, status=409)
    finally:
        temporary.unlink(missing_ok=True)

    await run_io(controller.library.scan)
    title = Path(filename).stem
    movie = next((m for m in controller.movies if m.title == title), None)
    if movie is None:
        # Scanned but didn't come back with this exact title - still saved,
        # just report it generically rather than failing the upload outright.
        return web.json_response(
            {"id": None, "title": title, "duration_seconds": 0, "description": None, "year": None},
            status=201,
        )

    return web.json_response(_movie_payload(controller, movie), status=201)


async def _get_thumbnail(request: web.Request) -> web.StreamResponse:
    controller = request.app[_CONTROLLER_KEY]
    try:
        movie_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid movie id"}, status=400)

    thumbnail_path = controller.library.thumbnail_path_for(movie_id)
    if thumbnail_path is None:
        return web.json_response({"error": "no thumbnail for this movie"}, status=404)
    return web.FileResponse(thumbnail_path)


async def _post_thumbnail(request: web.Request) -> web.Response:
    """Accepts a phone-supplied "official" thumbnail (e.g. from TMDB) and
    caches it, overwriting the local ffmpeg frame grab, so other devices
    that connect later get it too without needing their own internet access."""
    controller = request.app[_CONTROLLER_KEY]
    try:
        movie_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid movie id"}, status=400)

    if not any(movie.id == movie_id for movie in controller.movies):
        return web.json_response({"error": "unknown movie id"}, status=404)

    data = await request.read()
    if not data:
        return web.json_response({"error": "empty body"}, status=400)

    await run_io(controller.library.save_uploaded_thumbnail, movie_id, data)
    return web.json_response({"ok": True})


async def _post_metadata(request: web.Request) -> web.Response:
    """Accepts phone-supplied metadata (title/description/year/duration -
    e.g. fetched from TMDB) and merges it into what's cached for this movie,
    overriding the filename/ffprobe-derived values in GET /api/movies."""
    controller = request.app[_CONTROLLER_KEY]
    try:
        movie_id = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "invalid movie id"}, status=400)

    movie = next((m for m in controller.movies if m.id == movie_id), None)
    if movie is None:
        return web.json_response({"error": "unknown movie id"}, status=404)

    payload = await request.json()
    fields = {}
    for key in ("title", "description"):
        if key in payload:
            fields[key] = str(payload[key])
    for key in ("year", "duration_seconds"):
        if key in payload:
            try:
                fields[key] = int(payload[key])
            except (TypeError, ValueError):
                return web.json_response({"error": f"{key} must be an integer"}, status=400)

    if not fields:
        return web.json_response(
            {"error": f"body must include at least one of: {', '.join(_METADATA_FIELDS)}"}, status=400
        )

    await run_io(controller.library.save_metadata, movie_id, **fields)
    return web.json_response(_movie_payload(controller, movie))


async def _get_status(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER_KEY]
    state = await controller.refresh_status()
    throttle = await get_throttle_status()
    online = await internet_reachable()
    return web.json_response({
        "status": state.status.name.lower(),
        "movie_id": state.movie_id,
        "position_seconds": state.position_seconds,
        "syncing_movie_title": controller.currently_syncing_movie_title,
        "update_status": controller.update_status,
        "thermal_note": controller.thermal_note,
        "transcoding_movie_id": controller.currently_transcoding_movie_id,
        "poll_seconds": _poll_seconds(controller, state.status.name.lower() == "playing"),
        "cpu_temperature_celsius": cpu_temperature_celsius(),
        "under_voltage": throttle.under_voltage if throttle else None,
        "throttled": throttle.throttled if throttle else None,
        "internet_reachable": online,
    })


def _activity_payload(controller: PlaybackController) -> dict:
    """What the loading icon's panel shows: the download in flight and the
    ones behind it, and what the home server is still preparing."""
    activity = controller.sync_activity
    downloading = None
    if controller.currently_syncing_movie_title:
        downloading = {
            "title": controller.currently_syncing_movie_title,
            "bytes_done": activity.bytes_done if activity else None,
            "bytes_total": activity.bytes_total if activity else None,
        }

    home_server = None
    if activity is not None:
        home_server = {
            "reachable": activity.reachable,
            "reached_at": activity.reached_at,
            "preparing": list(activity.preparing),
        }
    transcoding = None
    movie_id = controller.currently_transcoding_movie_id
    movie = next((m for m in controller.movies if m.id == movie_id), None) if movie_id is not None else None
    if movie is not None:
        position = controller.transcode_position_seconds
        duration = movie.duration_seconds
        transcoding = {
            "movie_id": movie.id,
            "title": movie.title,
            "position_seconds": position,
            "duration_seconds": duration,
            "percent": min(100.0, round(position / duration * 100, 1)) if position is not None and duration else None,
            "paused": controller.transcode_paused,
        }
    return {
        "downloading": downloading,
        "download_queue": list(activity.queued) if activity else [],
        "transcoding": transcoding,
        "home_server": home_server,
        # Downloads carry on while a movie plays, slowed down.
        "paused_for_playback": not controller.is_idle,
    }


async def _get_activity(request: web.Request) -> web.Response:
    return web.json_response(_activity_payload(request.app[_CONTROLLER_KEY]))


async def _get_version(request: web.Request) -> web.Response:
    return web.json_response({"api_version": API_VERSION, "ip_address": local_ip()})


async def _post_command(request: web.Request) -> web.Response:
    controller = request.app[_CONTROLLER_KEY]
    payload = await request.json()
    opcode_name = str(payload.get("opcode", "")).lower()
    opcode = _OPCODE_BY_NAME.get(opcode_name)
    if opcode is None:
        return web.json_response({"error": f"unknown opcode {opcode_name!r}"}, status=400)

    argument = payload.get("argument")
    cmd = Command(opcode=opcode, argument=None if argument is None else int(argument))
    await controller.handle_command(cmd)
    return web.json_response({"ok": True})


async def _get_info(request: web.Request) -> web.Response:
    """Everything the web settings panel shows: network identity, hardware
    and software details, and live health (temperature, power, internet)."""
    controller = request.app[_CONTROLLER_KEY]
    info = await run_io(system_info.snapshot, getattr(controller.library, "root", None))
    throttle = await get_throttle_status()
    state = await controller.refresh_status()
    info.update({
        "api_version": API_VERSION,
        "ip_address": local_ip(),
        "internet_reachable": await internet_reachable(),
        "cpu_temperature_celsius": cpu_temperature_celsius(),
        "under_voltage": throttle.under_voltage if throttle else None,
        "throttled": throttle.throttled if throttle else None,
        "playback_status": state.status.name.lower(),
        "movie_count": len(controller.movies),
        "update_status": controller.update_status,
        "activity": controller.activity_message,
        "keyboards": list(controller.keyboard_names),
    })
    return web.json_response(info)


async def _get_logs(request: web.Request) -> web.Response:
    """Recent Wi-Fi, startup and update logs for the settings panel, so a
    device on its hotspot can be diagnosed from a phone without SSH."""
    source = request.query.get("source", "wifi")
    if source not in system_info.LOG_SOURCES:
        return web.json_response({"error": f"unknown log source {source!r}"}, status=400)
    try:
        lines = int(request.query.get("lines", system_info.LOG_LINES))
    except ValueError:
        return web.json_response({"error": "lines must be a number"}, status=400)
    lines = max(1, min(lines, system_info.MAX_LOG_LINES))
    return web.json_response(await run_io(system_info.logs, source, lines))


async def _post_reboot(request: web.Request) -> web.Response:
    error = await system_info.reboot()
    if error:
        return web.json_response({"error": error}, status=500)
    return web.json_response({"ok": True})


async def _post_update(request: web.Request) -> web.Response:
    error = await system_info.start_update()
    if error:
        return web.json_response({"error": error}, status=500)
    return web.json_response({"ok": True})


async def _get_wifi_networks(request: web.Request) -> web.Response:
    """Saved network names only - never the passwords."""
    try:
        networks = await run_io(load_networks)
    except (OSError, ValueError):
        return web.json_response({"error": "cannot read saved Wi-Fi networks"}, status=500)
    return web.json_response({"networks": [network["ssid"] for network in networks]})


async def _post_wifi_network(request: web.Request) -> web.Response:
    """Saves a network for later: it is joined by startup or the Wi-Fi search,
    not now, so the hotspot this page is using stays up."""
    try:
        payload = await request.json()
        ssid, password = payload["ssid"], payload.get("password", "")
        await run_io(save_network, ssid, password)
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "enter a network name (1-32 bytes) and a password"}, status=400)
    except OSError:
        return web.json_response({"error": "could not save the network"}, status=500)
    return web.json_response({"ok": True})


async def _delete_wifi_network(request: web.Request) -> web.Response:
    """Forgets a saved network. A connection to it now stays up; the device
    stops joining it from its next startup or Wi-Fi search."""
    try:
        ssid = (await request.json())["ssid"]
        if not isinstance(ssid, str):
            raise TypeError
        removed = await run_io(remove_network, ssid)
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "name the network to remove"}, status=400)
    except OSError:
        return web.json_response({"error": "could not remove the network"}, status=500)
    if not removed:
        return web.json_response({"error": "that network is not saved"}, status=404)
    return web.json_response({"ok": True})


async def _get_network(request: web.Request) -> web.Response:
    """The Networks tab: current connection, addresses and saved networks
    (names only, never passwords)."""
    data = await run_io(system_info.network)
    data["internet_reachable"] = await internet_reachable()
    return web.json_response(data)


async def _post_wifi_search(request: web.Request) -> web.Response:
    error = await system_info.search_wifi()
    if error:
        return web.json_response({"error": error}, status=500)
    return web.json_response({"ok": True})


async def _post_shutdown(request: web.Request) -> web.Response:
    error = await system_info.shutdown()
    if error:
        return web.json_response({"error": error}, status=500)
    return web.json_response({"ok": True})
