"""Transport-agnostic playback control: wraps the movie library and mpv,
independent of whether commands arrive over BLE or plain HTTP."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional

from ..update_status import MESSAGES as UPDATE_MESSAGES, read_status
from ..storage import read_dict, run_io, write_json
from ..views.idle_screen import IdleActivity, render_idle_screen
from ..models.library import MovieLibrary
from ..views.player import MpvController
from ..models.protocol import Command, Movie, Opcode, PlaybackState, PlaybackStatus

logger = logging.getLogger(__name__)

# How much to dim the picture (via MpvController.set_dim) while paused -
# separate from, and much lighter than, IdleDimService's dim for extended
# inactivity with nothing selected at all.
PAUSE_DIM_PERCENT = 10
PLAYBACK_SAVE_INTERVAL_SECONDS = 5
# Accent colors for the idle screen's activity card (see idle_activity).
_DOWNLOAD_COLOR = (245, 197, 66)
_TRANSCODE_COLOR = (90, 200, 250)
_UPDATE_COLOR = (255, 255, 255)


class PlaybackController:
    def __init__(self, library: MovieLibrary, player: MpvController, state_path: Optional[Path] = None):
        self.library = library
        self.player = player
        self._current_movie_id: Optional[int] = None
        self._playback_lock: Optional[asyncio.Lock] = None
        self._render_lock: Optional[asyncio.Lock] = None
        self._startup_input_at = time.monotonic()
        # Where the currently-selected movie/position/paused-ness gets
        # persisted (see _save_playback_state/restore_last_playback) so a
        # power loss or reboot can pick back up instead of dropping back to
        # the idle screen. None (the default, and what every existing test
        # here gets) disables persistence entirely rather than writing
        # somewhere real - main.py is the only caller that passes one.
        self._last_saved_state = None
        self._last_checkpoint_at = None
        self._last_checkpoint_movie_id = None
        self._state_path = state_path
        self._positions_path = state_path.with_name("movie_positions.json") if state_path else None
        raw_positions = read_dict(self._positions_path) if self._positions_path else {}
        self._positions = {key: value for key, value in raw_positions.items()
                           if key.isdecimal() and type(value) is int and 0 <= value <= 0xFFFFFFFF}
        self._software_update_phase = None
        # Which movie the SD idle screen's slideshow is on (see idle_screen.py).
        self.slide_index = 0

        # Set/cleared by HomeServerSync, read by web_service.py's /api/status
        # A title, not an id: the movie doesn't have a local id yet while
        # it's still downloading (library._stable_id only ever runs against
        # files that already exist on disk).
        self.currently_syncing_movie_title: Optional[str] = None
        # True while the home server has movies to download and this device
        # is fetching them.
        self.sync_busy: bool = False
        # HomeServerSync's SyncActivity (queue, bytes, home server's own
        # transcodes), set by main.py; None when home sync isn't running.
        self.sync_activity = None
        # Names of attached keyboards, maintained by KeyboardService and
        # shown in the idle screen's footer hint.
        self.keyboard_names: list[str] = []
        # Startup/update progress text (see update_status.read_message),
        # kept fresh by main._run_status_message and drawn on the idle screen.
        self.status_message: Optional[str] = None
        # Set by ThermalService while the device is too hot (playback paused).
        self.thermal_note: Optional[str] = None
        # Set/cleared by TranscodeService: the movie being re-encoded here,
        # how far the encode has got, and whether it's paused (playing,
        # downloading or too hot).
        self.currently_transcoding_movie_id: Optional[int] = None
        self.transcode_position_seconds: Optional[float] = None
        self.transcode_paused: bool = False
        # Updated on every command (remote or local) - IdleDimService reads
        # this to know how long it's been since anything happened, so it
        # knows when to dim the idle screen. monotonic(), not wall-clock
        # time, since it only needs to measure elapsed duration and can't
        # be upset by clock adjustments.
        self.last_input_at: float = self._startup_input_at

    @property
    def home_server_has_work(self) -> bool:
        """True while movies are downloading from the home server, or it is
        reachable and still has movies for this device: queued to download,
        or being made into the 480p copy this device downloads. Background
        transcoding waits for all of that - downloads come first."""
        if self.sync_busy:
            return True
        activity = self.sync_activity
        return bool(activity and activity.reachable and (activity.queued or activity.preparing))

    @property
    def activity_message(self) -> Optional[str]:
        """What the device is busy doing, for the big banner on the idle
        screen: update/internet progress first, then downloads and
        transcodes. No percentages, so it only changes (and the idle screen
        is only redrawn for it) when the step or the movie changes."""
        if self.status_message:
            return self.status_message
        if self.thermal_note:
            return self.thermal_note
        activity = self.idle_activity
        if activity is None:
            return None
        return f"{activity.heading} {activity.title}"

    @property
    def idle_activity(self) -> Optional[IdleActivity]:
        """What the idle screen shows in place of the slideshow, in priority
        order: a software update being installed (the player restarts when
        it's done), a download from the media server, a transcode on this
        device, then the media server's own transcode of a movie this device
        is waiting for, then downloads still waiting to start. None once
        everything is done: the slideshow."""
        if self.is_updating:
            return IdleActivity("Updating", "Device software", None,
                                "MagicBoxie restarts when it's done", color=_UPDATE_COLOR)

        activity = self.sync_activity
        title = self.currently_syncing_movie_title
        if title:
            percent = None
            if activity and activity.bytes_total:
                percent = min(100, activity.bytes_done * 100 // activity.bytes_total)
            queued = len(activity.queued) if activity else 0
            return IdleActivity("Downloading", title, percent,
                                f"{queued} more to download" if queued else None,
                                color=_DOWNLOAD_COLOR)

        movie_id = self.currently_transcoding_movie_id
        if movie_id is not None:
            movie = next((m for m in self.movies if m.id == movie_id), None)
            percent = None
            position = self.transcode_position_seconds
            if movie and movie.duration_seconds and position is not None:
                percent = min(100, int(position * 100 // movie.duration_seconds))
            return IdleActivity("Transcoding", movie.title if movie else "a movie", percent,
                                "Paused until the device cools down" if self.transcode_paused else "On this player",
                                movie_id=movie_id, color=_TRANSCODE_COLOR)

        if activity and activity.reachable and activity.preparing:
            preparing = activity.preparing
            item = next((p for p in preparing if p.get("status") == "transcoding"), preparing[0])
            transcoding = item.get("status") == "transcoding"
            percent = item.get("progress_percent")
            others = len(preparing) - 1
            detail = "On the media server" + (f", {others} more waiting" if others else ", downloads when ready")
            return IdleActivity(
                "Transcoding" if transcoding else "Preparing",
                item.get("title") or "a movie",
                int(percent) if transcoding and percent is not None else None,
                detail,
                color=_TRANSCODE_COLOR,
            )

        # Work that's waiting rather than running - downloads between
        # check-ins - still isn't "everything done", so it keeps the card up
        # instead of dropping back to the slideshow.
        if activity and activity.reachable and activity.queued:
            others = len(activity.queued) - 1
            return IdleActivity("Downloading", activity.queued[0], None,
                                "Waiting to start" + (f", {others} more after it" if others else ""),
                                color=_DOWNLOAD_COLOR)
        return None

    @property
    def is_updating(self) -> bool:
        return (self._software_update_phase == "installing"
                or self.status_message == UPDATE_MESSAGES["installing"])

    @property
    def update_status(self) -> Optional[str]:
        phase = self._software_update_phase
        if phase == "installing":
            return "Updating device software"
        if self.currently_syncing_movie_title:
            return "Updating movies"
        return None

    @property
    def _lock(self) -> asyncio.Lock:
        # Python 3.9 binds locks to the loop at construction time.
        if self._playback_lock is None:
            self._playback_lock = asyncio.Lock()
        return self._playback_lock

    @property
    def movies(self) -> List[Movie]:
        return self.library.movies

    @property
    def is_idle(self) -> bool:
        """Whether anything is currently selected to play - background work
        (library scans, the idle screen) waits for this, and downloads slow
        down, so they don't compete with playback decode on the single core."""
        return self._current_movie_id is None

    async def handle_command(self, cmd: Command) -> None:
        self.last_input_at = time.monotonic()
        async with self._lock:
            await self._handle_command(cmd)

    async def _handle_command(self, cmd: Command) -> None:
        if cmd.opcode == Opcode.SELECT_MOVIE and cmd.argument is not None:
            if not any(movie.id == cmd.argument for movie in self.movies):
                # A client's cached movie list can be stale relative to what
                # the library currently has (e.g. mid-rescan, or a movie
                # removed since) - not selecting anything is a much better
                # failure mode than an unhandled KeyError deep in
                # library.playable_path_for taking the whole request down.
                logger.warning("Ignoring select_movie for unknown movie id %d", cmd.argument)
                return
            await self._remember_position()
            self._current_movie_id = cmd.argument
            await self.player.load(self.library.playable_path_for(cmd.argument),
                                   start_seconds=self._resume_position(cmd.argument))
            await self.player.hide_pause_icon()
            # Immediate feedback rather than waiting for IdleDimService's
            # next poll to notice is_idle flipped and undo an idle dim.
            await self.player.set_dim(0)
        elif cmd.opcode == Opcode.PLAY:
            await self.player.play()
            await self.player.hide_pause_icon()
            await self.player.set_dim(0)
        elif cmd.opcode == Opcode.PAUSE:
            await self.player.pause()
            await self.player.show_pause_icon()
            await self.player.set_dim(PAUSE_DIM_PERCENT)
        elif cmd.opcode == Opcode.STOP:
            await self._stop_and_show_idle_screen(stopped_by_user=True)
        elif cmd.opcode == Opcode.SEEK and cmd.argument is not None:
            await self.player.seek(cmd.argument)
        elif cmd.opcode == Opcode.SHUTDOWN:
            await self._shutdown()

    @staticmethod
    async def _shutdown() -> None:
        """Powers off the whole device. Not really "playback control", but
        routed through the same command channel as everything else since
        there's no separate system-command pathway and this is the only
        such action that exists. Needs sudo since the service itself runs
        unprivileged (see system/magicboxie-player.service.in) - relies on
        the Pi's default passwordless sudo for the setup user rather than
        provisioning a narrower rule, since that's already how this
        specific device is configured."""
        await asyncio.create_subprocess_exec("sudo", "-n", "/usr/bin/systemctl", "poweroff")

    async def show_idle_screen(self) -> None:
        """Displays the thumbnail-grid home screen - the device's resting
        state whenever nothing is selected to play (at startup, and after
        stop_and_show_idle_screen()). Also the live "syncing" badge's only
        home: it's drawn into this same image (see idle_screen.py) since
        mpv can only ever show one static file at a time, not a separate
        overlay layer on top of it."""
        if not self.is_idle:
            return
        image_path = await self._render_idle_screen()
        async with self._lock:
            if self.is_idle:
                await self.player.show_image(image_path)

    async def advance_slideshow(self) -> None:
        """Steps the SD idle screen on to the next movie's slide. While a
        download or transcode is shown instead, stays put and just redraws,
        which keeps its progress bar current."""
        if not self.is_idle:
            return
        if self.idle_activity is None:
            self.slide_index = (self.slide_index + 1) % max(1, len(self.library.movies))
        await self.show_idle_screen()

    async def _render_idle_screen(self) -> Path:
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        async with self._render_lock:
            return await run_io(render_idle_screen, self.library,
                                syncing_title=self.currently_syncing_movie_title,
                                keyboard_names=list(self.keyboard_names),
                                # The update card already says it.
                                status_message=None if self.is_updating else self.status_message,
                                slide_index=self.slide_index,
                                activity=self.idle_activity,
                                positions={movie.id: self.saved_position(movie.id)
                                           for movie in self.movies})

    async def _show_idle_screen_locked(self) -> None:
        image_path = await self._render_idle_screen()
        await self.player.show_image(image_path)

    async def stop_and_show_idle_screen(self) -> None:
        self.last_input_at = time.monotonic()
        async with self._lock:
            await self._stop_and_show_idle_screen(stopped_by_user=True)

    async def _stop_and_show_idle_screen(self, stopped_by_user: bool = False) -> None:
        """What an explicit stop command does, and what an update install
        does to whatever is playing - stop it and return to the idle
        screen, so the screen never just goes blank or freezes on the last
        frame. The movie's exact position stays saved either way (the
        slideshow marks it with a progress bar and selecting it resumes
        there). After an update restart the device resumes that movie; one
        the user stopped is not resumed at boot, the slideshow shows instead.
        Only one that played to its end or failed is forgotten."""
        movie_id = self._current_movie_id
        if movie_id is not None and not (self.player.finished or self.player.failed or self.player.loading):
            position = await self.player.get_position()
            self._last_checkpoint_at = None
            await run_io(self._save_playback_state, movie_id, position, False, stopped_by_user)
        else:
            await run_io(self._clear_playback_state)
        await self._remember_position()
        await self.player.stop()
        self._current_movie_id = None
        # The pause icon/dim are a separate overlay layer from whatever's
        # loaded, so stopping while paused would otherwise leave them
        # visible over the idle screen.
        await self.player.hide_pause_icon()
        await self.player.set_dim(0)
        await self._show_idle_screen_locked()

    async def resume_on_startup(self) -> None:
        """Once the library is ready, unless startup input took priority,
        continue whatever was playing before the device last stopped (power
        loss, reboot, update restart) at its saved position. With nothing to
        continue, show the idle screen rather than picking a movie."""
        async with self._lock:
            if self.last_input_at != self._startup_input_at or not self.is_idle:
                return
            if await self.restore_last_playback():
                return
            await run_io(self._clear_playback_state)
            await self._show_idle_screen_locked()

    async def refresh_status(self) -> PlaybackState:
        async with self._lock:
            return await self._refresh_status()

    async def _refresh_status(self) -> PlaybackState:
        self._software_update_phase = await run_io(read_status)
        # Nothing selected - already known to be idle without asking mpv,
        # which would otherwise report "not idle" while the idle-screen
        # image itself is loaded (see MpvController.show_image). Persisted
        # state was already cleared at the point _current_movie_id became
        # None (below, or in stop_and_show_idle_screen), so there's nothing
        # left to do here.
        if self._current_movie_id is None:
            return PlaybackState.idle()

        if self._software_update_phase == "installing":
            await self._stop_and_show_idle_screen()
            return PlaybackState.idle()

        if self.player.failed:
            failed_id = self._current_movie_id
            retry_original = False
            if self.player.media_failed:
                retry_original = await run_io(self.library.quarantine_failed_playback, failed_id)
            if retry_original:
                await self.player.load(self.library.playable_path_for(failed_id),
                                       start_seconds=self._resume_position(failed_id))
            else:
                logger.warning("Stopping movie %s after playback failure", failed_id)
                self._current_movie_id = None
                await self._stop_and_show_idle_screen()
                return PlaybackState.idle()

        if self.player.finished:
            previous_movie_id = self._current_movie_id
            await run_io(self._set_position, previous_movie_id, 0)
            # Do not save the last frame again; return to the idle screen.
            self._current_movie_id = None
            await self._stop_and_show_idle_screen()
            return PlaybackState.idle()

        idle = await self.player.get_idle()
        if idle:
            self._current_movie_id = None
            await run_io(self._clear_playback_state)
            return PlaybackState.idle()

        if self.player.loading:
            return PlaybackState(status=PlaybackStatus.PLAYING, movie_id=self._current_movie_id,
                                 position_seconds=self._resume_position(self._current_movie_id))

        paused = await self.player.get_paused()
        position = await self.player.get_position()
        # Status stays live; disk checkpoints are limited to every five seconds.
        await run_io(self._save_playback_state, self._current_movie_id, position, paused)
        return PlaybackState(
            status=PlaybackStatus.PAUSED if paused else PlaybackStatus.PLAYING,
            movie_id=self._current_movie_id,
            position_seconds=position,
        )

    def saved_position(self, movie_id: int) -> int:
        """Where this movie would resume, in seconds: 0 if it was never
        played or was last played to its end. The web page draws its
        "previously watched" progress bar from this."""
        return self._resume_position(movie_id)

    def _resume_position(self, movie_id: int) -> int:
        position = self._positions.get(str(movie_id), 0)
        movie = next((movie for movie in self.movies if movie.id == movie_id), None)
        return 0 if movie and movie.duration_seconds and position >= movie.duration_seconds else position

    def _set_position(self, movie_id: int, position: int) -> None:
        position = max(0, min(position, 0xFFFFFFFF))
        key = str(movie_id)
        if self._positions.get(key) == position:
            return
        self._positions[key] = position
        if self._positions_path:
            try:
                write_json(self._positions_path, self._positions)
            except OSError:
                logger.warning("Could not save movie positions")

    async def save_position_now(self) -> None:
        """Writes the current movie's exact position and resume state, for a
        shutdown or restart (otherwise it could be up to a checkpoint stale)."""
        movie_id = self._current_movie_id
        if movie_id is None or self.player.finished or self.player.failed or self.player.loading:
            return
        position = await self.player.get_position()
        paused = await self.player.get_paused()
        self._last_checkpoint_at = None
        await run_io(self._save_playback_state, movie_id, position, paused)

    async def _remember_position(self) -> None:
        if self._current_movie_id is not None and not self.player.finished and not self.player.failed and not self.player.loading:
            position = await self.player.get_position()
            await run_io(self._set_position, self._current_movie_id, position)

    def _save_playback_state(self, movie_id: int, position_seconds: int, paused: bool,
                             stopped: bool = False) -> None:
        now = time.monotonic()
        if (self._last_checkpoint_movie_id == movie_id
                and self._last_checkpoint_at is not None
                and now - self._last_checkpoint_at < PLAYBACK_SAVE_INTERVAL_SECONDS):
            return
        self._last_checkpoint_at = now
        self._last_checkpoint_movie_id = movie_id
        self._set_position(movie_id, position_seconds)
        state = (movie_id, position_seconds, paused, stopped)
        if self._state_path is None or state == self._last_saved_state:
            return
        data = {"movie_id": movie_id, "position_seconds": position_seconds, "paused": paused}
        if stopped:
            # Stopped by the user: boot shows the slideshow, not this movie.
            data["stopped"] = True
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self._state_path, data)
            self._last_saved_state = state
        except OSError:
            logger.warning("Failed to persist playback state to %s", self._state_path)

    def _clear_playback_state(self) -> None:
        self._last_saved_state = None
        self._last_checkpoint_at = None
        self._last_checkpoint_movie_id = None
        if self._state_path is None:
            return
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to clear persisted playback state at %s", self._state_path)

    async def restore_last_playback(self) -> bool:
        """Resumes whatever was selected the last time the device ran - see
        _save_playback_state above for where this gets written, continuously,
        while something's selected. Meant to be called once at startup,
        after the library has been scanned (a persisted movie id only means
        anything once it can be checked against a freshly-scanned library -
        see main.py's _run_library_scan). Returns whether it actually
        resumed something, so the caller knows whether to fall back to
        show_idle_screen() itself."""
        if self._state_path is None or not self._state_path.is_file():
            return False

        try:
            data = await run_io(read_dict, self._state_path)
            movie_id = int(data["movie_id"])
            position_seconds = int(data["position_seconds"])
            paused = bool(data["paused"])
            stopped = bool(data.get("stopped", False))
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Failed to read persisted playback state from %s - ignoring", self._state_path)
            await run_io(self._clear_playback_state)
            return False

        if stopped:
            # Its position is still in movie_positions.json, so the slideshow
            # marks it and selecting it resumes where it was stopped.
            logger.info("Last movie %d was stopped - starting on the idle screen", movie_id)
            return False

        if not any(movie.id == movie_id for movie in self.movies):
            # The movie was deleted, or the library changed, since this was
            # written - same "stale reference" handling as an unknown
            # SELECT_MOVIE id in handle_command.
            logger.info("Persisted playback state names movie %d, no longer in the library - ignoring", movie_id)
            await run_io(self._clear_playback_state)
            return False

        self._current_movie_id = movie_id
        await self.player.load(
            self.library.playable_path_for(movie_id),
            start_seconds=position_seconds,
            paused=paused,
        )
        if paused:
            await self.player.show_pause_icon()
            await self.player.set_dim(PAUSE_DIM_PERCENT)
        else:
            await self.player.set_dim(0)
        return True
