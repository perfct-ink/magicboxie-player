"""Opportunistic sync with the home server (MagicBoxie-web): when the device
has internet, downloads any "ready" movies it doesn't have locally yet,
along with their metadata (title/description/year/duration). The device
spends most of its life offline, so a failed check-in (server unreachable,
DNS failure, etc.) is the normal case, not an error - see _run_home_sync in
main.py, which retries on an interval and just logs and moves on.

MagicBoxie-web speaks the real Jellyfin REST API (see its
internal/controllers/items_controller.go) plus a handful of additive
"MagicBoxie*"-prefixed fields real Jellyfin clients don't have -- status,
progress, and original filename -- which this sync relies on to decide
what's actually downloadable and how to name the local file.

Thumbnails aren't fetched here - once a movie's file lands locally, the next
MovieLibrary.scan() grabs a frame from it with ffmpeg like any other movie.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import aiohttp

from ..storage import publish_file, run_io
from ..models.library import VIDEO_EXTENSIONS, MovieLibrary

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# While a movie plays, downloads are capped to this rate so they leave the
# CPU, disk and Wi-Fi to playback (priority: playback, then downloads). Idle, they run flat out.
_PLAYING_DOWNLOAD_BYTES_PER_SECOND = 1024 * 1024
_READY_STATUS = "ready"
_USER_ID = "1"  # MagicBoxie-web has one shared login, not per-user accounts
_TICKS_PER_SECOND = 10_000_000  # Jellyfin's RunTimeTicks unit is 100ns
# The home server makes a 480p copy of each synced movie for this device
# (MagicBoxiePlayerStatus, served from /Videos/{id}/player), so the Pi
# doesn't have to re-encode it. Until it's "ready" the movie waits; if the
# server couldn't make one ("error"), or is too old to say (no field at all),
# the full-size file is downloaded and played as it is.
_PLAYER_COPY_READY = "ready"
_PLAYER_COPY_FAILED = "error"
# The server versions its player copy encoding (MagicBoxiePlayerVersion); the
# version of what's on disk is kept in the movie's metadata (0 for a
# full-size original, or anything downloaded before versions existed). A
# movie whose server copy is newer is downloaded again and swapped in, so
# originals and copies made with an older encoding don't stay on the device.
_PLAYER_VERSION_KEY = "player_copy_version"


@dataclass
class SyncActivity:
    """What the home sync is doing, for the web page's activity panel:
    the download in flight (title via on_progress, bytes here), the movies
    waiting behind it, and the home server's own not-yet-ready movies."""
    # Titles still to download after the current one, in download order.
    queued: List[str] = field(default_factory=list)
    bytes_done: int = 0
    bytes_total: Optional[int] = None
    # Movies marked for this device that the home server is still
    # preparing: [{"title", "status", "progress_percent"}].
    preparing: List[dict] = field(default_factory=list)
    # time.time() of the last check-in that reached the home server.
    reached_at: Optional[float] = None
    reachable: bool = False


class HomeServerSync:
    def __init__(
        self,
        library: MovieLibrary,
        base_url: str,
        password: str,
        on_progress: Optional[Callable[[Optional[str]], None]] = None,
        is_idle: Callable[[], bool] = lambda: True,
        on_busy: Optional[Callable[[bool], None]] = None,
    ):
        self._library = library
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._device_id = self._read_device_identifier()
        # Called with a movie's title when its download starts and None
        # when it ends (success, failure, or exception alike) - lets
        # main.py mirror "what's downloading right now" onto
        # PlaybackController for web_service.py's /api/status to report,
        # without this class needing to know PlaybackController exists.
        self._on_progress = on_progress or (lambda _title: None)
        # Downloads run regardless; only probing/scanning new files waits
        # until idle so it does not compete with video decoding.
        self._is_idle = is_idle
        # True from the moment there are movies to download until the check-in
        # ends (shown on the activity card).
        self._on_busy = on_busy or (lambda _busy: None)
        # Movies downloaded in an earlier check-in whose library.scan() (and
        # metadata save) got deferred because playback started before this
        # cycle reached that point - retried on a later, idle cycle rather
        # than dropped, so a movie that finished downloading right as
        # someone hit play doesn't end up stuck unscanned (visible in
        # `movies` with no duration/artwork) or missing its title/year/
        # description from the home server indefinitely.
        self._pending_metadata: List[dict] = []
        self.activity = SyncActivity()

    async def check_in(self) -> None:
        try:
            await self._check_in()
        finally:
            self._on_busy(False)

    async def _check_in(self) -> None:
        async with aiohttp.ClientSession() as session:
            try:
                remote_movies = await self._register_and_list_movies(session)
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.info("Home server check-in: couldn't list movies (%s)", exc)
                self.activity.reachable = False
                self.activity.queued = []
                self.activity.preparing = []
                return

            local_ids = {movie.title: movie.id for movie in self._library.movies}
            to_download = [m for m in remote_movies
                           if m["Name"] not in local_ids and not self._waiting_for_player_copy(m)]
            # New movies first; replacing ones already here can wait.
            to_download += [m for m in remote_movies
                            if m["Name"] in local_ids and self._has_newer_copy(m, local_ids[m["Name"]])]
            self.activity.queued = [m["Name"] for m in to_download]

            # Downloads take priority over everything else, playback included:
            # they run whether or not a movie is playing. Only the library
            # scan below waits for idle (it forks ffmpeg for each new file).
            if to_download:
                self._on_busy(True)
            if to_download:
                # /devices/register (the common path above) lists movies
                # without authenticating, but the video bytes themselves are
                # served from an authenticated route - unlike the metadata-
                # only registration listing, anonymous access to stream
                # arbitrary video would be a real information-disclosure
                # concern. A token is needed here regardless of which path
                # found the movies to download.
                token = await self._authenticate(session)
                if token is None:
                    logger.info(
                        "Home server check-in: found %d new movie(s) but couldn't authenticate to download them",
                        len(to_download),
                    )
                else:
                    headers = {"Authorization": f"Bearer {token}"}
                    logger.info("Home server check-in: downloading %d new movie(s)", len(to_download))
                    # One at a time, deliberately: these are large files over
                    # a slow WiFi/USB-tethered link, and _on_progress only
                    # ever tracks a single in-flight title - downloading
                    # several concurrently would also just contend with each
                    # other for the same bandwidth with nothing gained.
                    for index, movie in enumerate(to_download):
                        self.activity.queued = [m["Name"] for m in to_download[index + 1:]]
                        self._on_progress(movie["Name"])
                        try:
                            if await self._download_movie(session, movie, headers, replace_id=local_ids.get(movie["Name"])):
                                self._pending_metadata.append(movie)
                        finally:
                            self.activity.bytes_done, self.activity.bytes_total = 0, None
                            self._on_progress(None)
                    else:
                        self.activity.queued = []

            if not self._pending_metadata:
                if not to_download:
                    logger.info("Home server check-in: nothing new")
                return

            if not self._is_idle():
                logger.info("Home server check-in: deferring library scan until playback stops")
                return

            await run_io(self._library.scan)
            for remote_movie in self._pending_metadata:
                await run_io(self._save_metadata, remote_movie)
            self._pending_metadata = []

    async def _register_and_list_movies(self, session: aiohttp.ClientSession) -> List[dict]:
        try:
            async with session.post(
                f"{self._base_url}/devices/register",
                json={"device_id": self._device_id},
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("items") or data.get("Items") or []
                    self._reached([
                        {
                            "title": item.get("Name") or "",
                            "status": item.get("Status") or "",
                            "progress_percent": item.get("ProgressPercent"),
                        }
                        for item in data.get("MagicBoxiePreparing") or []
                    ])
                    return [self._normalize_registered_movie(item) for item in items]
                logger.info("Home server check-in: registration endpoint responded HTTP %d", resp.status)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.info("Home server check-in: registration endpoint unavailable (%s)", exc)

        token = await self._authenticate(session)
        if token is None:
            return []
        headers = {"Authorization": f"Bearer {token}"}
        movies = await self._list_ready_movies(session, headers)
        self._reached([])
        return movies

    def _reached(self, preparing: List[dict]) -> None:
        self.activity.reachable = True
        self.activity.reached_at = time.time()
        self.activity.preparing = preparing

    async def _authenticate(self, session: aiohttp.ClientSession):
        try:
            async with session.post(
                f"{self._base_url}/Users/AuthenticateByName",
                json={"Username": "magicboxie-device", "Pw": self._password},
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.info("Home server check-in: login failed (HTTP %d)", resp.status)
                    return None
                data = await resp.json()
                return data.get("AccessToken")
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.info("Home server check-in: unreachable (%s)", exc)
            return None

    async def _list_ready_movies(self, session: aiohttp.ClientSession, headers: dict) -> List[dict]:
        async with session.get(
            f"{self._base_url}/Users/{_USER_ID}/Items",
            params={"IncludeItemTypes": "Movie", "Recursive": "true"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        items = data.get("Items", [])
        return [item for item in items if item.get("MagicBoxieStatus") == _READY_STATUS]

    async def _download_movie(self, session: aiohttp.ClientSession, movie: dict, headers: dict,
                              replace_id: Optional[int] = None) -> bool:
        """Downloads a movie into the library, or with replace_id, downloads
        a newer player copy and swaps it in for that movie's file."""
        dest_path = self._library.root / self._local_filename(movie)
        if dest_path.exists() and replace_id is None:
            return False

        logger.info("Home server check-in: downloading %r", movie["Name"])
        temporary = dest_path.with_name("." + dest_path.name + ".partial")
        if self._is_player_copy(movie):
            url, params = f"{self._base_url}/Videos/{movie['Id']}/player", None
        else:
            url, params = f"{self._base_url}/Videos/{movie['Id']}/stream", {"static": "true"}
        try:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Home server check-in: download of %r failed (HTTP %d)", movie["Name"], resp.status)
                    return False
                self.activity.bytes_done, self.activity.bytes_total = 0, resp.content_length
                with temporary.open("wb") as f:
                    chunk_started = time.monotonic()
                    async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                        await run_io(f.write, chunk)
                        self.activity.bytes_done += len(chunk)
                        if not self._is_idle():
                            # Playing: sleep off whatever of this chunk's
                            # time budget the network didn't already use.
                            budget = len(chunk) / _PLAYING_DOWNLOAD_BYTES_PER_SECOND
                            await asyncio.sleep(max(0.0, budget - (time.monotonic() - chunk_started)))
                        chunk_started = time.monotonic()
                if temporary.stat().st_size == 0:
                    return False
                if replace_id is None:
                    await run_io(publish_file, temporary, dest_path)
                else:
                    await run_io(self._library.replace_file, replace_id, temporary, dest_path.name)
                    # Recorded now, not at the (possibly deferred) scan, so
                    # the next check-in doesn't download it yet again.
                    await run_io(self._library.save_metadata, replace_id,
                                 **{_PLAYER_VERSION_KEY: self._player_version(movie)})
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.warning("Home server check-in: download of %r failed (%s)", movie["Name"], exc)
            return False
        finally:
            temporary.unlink(missing_ok=True)
        return True

    @staticmethod
    def _waiting_for_player_copy(movie: dict) -> bool:
        status = movie.get("MagicBoxiePlayerStatus")
        return status is not None and status not in (_PLAYER_COPY_READY, _PLAYER_COPY_FAILED)

    @staticmethod
    def _is_player_copy(movie: dict) -> bool:
        return movie.get("MagicBoxiePlayerStatus") == _PLAYER_COPY_READY

    @classmethod
    def _player_version(cls, movie: dict) -> int:
        """Version of the copy a download of this movie gets; 0 for the original."""
        if not cls._is_player_copy(movie):
            return 0
        return int(movie.get("MagicBoxiePlayerVersion") or 0)

    def _has_newer_copy(self, movie: dict, local_id: int) -> bool:
        local_version = self._library.metadata_for(local_id).get(_PLAYER_VERSION_KEY) or 0
        return self._player_version(movie) > local_version

    def _save_metadata(self, remote_movie: dict) -> None:
        local_movie = next(
            (m for m in self._library.movies if m.title == remote_movie["Name"]), None
        )
        if local_movie is None:
            return
        self._library.save_metadata(
            local_movie.id,
            title=remote_movie["Name"],
            description=remote_movie.get("Overview") or "",
            year=int(remote_movie.get("ProductionYear") or 0),
            duration_seconds=int((remote_movie.get("RunTimeTicks") or 0) / _TICKS_PER_SECOND),
            **{_PLAYER_VERSION_KEY: self._player_version(remote_movie)},
        )

    @staticmethod
    def _normalize_registered_movie(movie: dict) -> dict:
        return {
            "Id": movie.get("Id") or movie.get("id"),
            "Name": movie.get("Name") or movie.get("name"),
            "Overview": movie.get("Overview") or movie.get("description") or "",
            "ProductionYear": movie.get("ProductionYear") or movie.get("year") or 0,
            "RunTimeTicks": movie.get("RunTimeTicks") or movie.get("duration_seconds", 0) * _TICKS_PER_SECOND,
            "MagicBoxieOriginalFilename": movie.get("MagicBoxieOriginalFilename") or movie.get("filename") or "",
            "MagicBoxieStatus": movie.get("MagicBoxieStatus") or movie.get("status") or _READY_STATUS,
            "MagicBoxiePlayerStatus": movie.get("MagicBoxiePlayerStatus"),
            "MagicBoxiePlayerVersion": movie.get("MagicBoxiePlayerVersion") or 0,
        }

    @staticmethod
    def _read_device_identifier() -> str:
        override = os.environ.get("MAGICBOXIE_DEVICE_ID")
        if override:
            return override

        serial_path = Path("/sys/firmware/devicetree/base/serial-number")
        if serial_path.exists():
            try:
                return serial_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        cpuinfo_path = Path("/proc/cpuinfo")
        if cpuinfo_path.exists():
            try:
                for line in cpuinfo_path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("Serial"):
                        return line.split(":", 1)[1].strip()
            except OSError:
                pass

        machine_id_path = Path("/etc/machine-id")
        if machine_id_path.exists():
            try:
                return machine_id_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        return "unknown-device"

    @classmethod
    def _local_filename(cls, movie: dict) -> str:
        original = Path(movie.get("MagicBoxieOriginalFilename") or "")
        extension = original.suffix.lower() if original.suffix.lower() in VIDEO_EXTENSIONS else ".mp4"
        if cls._is_player_copy(movie):
            extension = ".mp4"
        # Titles come from the home server's own TMDB matching/filename
        # parsing and may contain "/" (e.g. "Fast/Furious") - not valid in a
        # single path component, so it gets swapped for a dash.
        safe_title = movie["Name"].replace("/", "-").lstrip(".")
        return f"{safe_title}{extension}"
