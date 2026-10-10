"""Drives mpv over its JSON IPC socket. See https://mpv.io/manual/master/#json-ipc."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# How long to wait for mpv to create its IPC socket before giving up. Was
# 5s, which turned out too tight under real-world load: DRM/KMS
# initialization on a Pi Zero W can legitimately take upwards of 15s (e.g.
# right after a reboot or a burst of other activity, like a systemd restart
# storm - a slow startup that trips this timeout kills the whole daemon,
# which then immediately retries and adds more load, making the next
# startup even slower). Confirmed via a manual run that mpv itself starts
# cleanly - it just needed more time, not a different invocation.
MPV_STARTUP_TIMEOUT_SECONDS = 30

# Virtual canvas mpv scales OSD overlay drawings onto, independent of the
# actual output resolution - arbitrary but must match between show/hide
# calls (mpv identifies an overlay by id, but a mismatched res would
# reposition/rescale it).
_OSD_RES_X = 1280
_OSD_RES_Y = 720
_PAUSE_ICON_OVERLAY_ID = 1
# Two bars drawn as ASS vector shapes (rather than a Unicode "⏸" character)
# so this doesn't depend on whatever font mpv falls back to actually having
# that glyph. \an7\pos(40,600) anchors the drawing's own top-left origin
# there, placing it near the bottom-left of the 1280x720 canvas.
_PAUSE_ICON_ASS = (
    r"{\an7\pos(40,600)\1c&HFFFFFF&\bord0\shad0\p1}"
    r"m 0 0 l 24 0 l 24 80 l 0 80 "
    r"m 40 0 l 64 0 l 64 80 l 40 80"
    r"{\p0}"
)

# Applied over IPC once mpv is up, so the Pi Zero keeps up with playback
# (a file that's too heavy otherwise stutters and drifts out of sync with its
# audio). Set as properties rather than command-line flags: an mpv that
# doesn't know one just reports an error for it and carries on, where an
# unknown flag would stop mpv from starting at all.
PLAYBACK_TUNING = {
    # Hardware H.264 decode where mpv considers it safe; software otherwise.
    "hwdec": "auto-safe",
    # When decoding falls behind, drop frames (in the decoder too, not just
    # at display) so the picture catches up with the audio instead of
    # drifting further behind it.
    "framedrop": "decoder+vo",
    # Skip H.264 deblocking on all but keyframes: the costliest part of
    # software decode, and hard to see at 480p on the car's screen.
    "vd-lavc-skiploopfilter": "nonkey",
    "vd-lavc-fast": True,
    # The cheapest scalers and no dithering, for the Pi's small GPU.
    "scale": "bilinear",
    "dscale": "bilinear",
    "cscale": "bilinear",
    "dither-depth": "no",
    "correct-downscaling": False,
    "linear-downscaling": False,
    "sigmoid-upscaling": False,
}


class MpvController:
    def __init__(self, socket_path: str = "/tmp/magicboxie-mpv.sock", extra_args: Optional[List[str]] = None):
        self.media_failed = False
        self.failed = False
        self.finished = False
        self._loading = False
        self._socket_path = socket_path
        self._extra_args = extra_args or []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._request_id = itertools.count(1)
        self._pending: Dict[int, "asyncio.Future"] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    @property
    def loading(self) -> bool:
        return self._loading

    async def start(self) -> None:
        socket_path = Path(self._socket_path)
        if socket_path.exists():
            socket_path.unlink()

        self._process = await asyncio.create_subprocess_exec(
            "mpv",
            "--idle=yes",
            "--force-window=no",
            "--no-terminal",
            f"--input-ipc-server={self._socket_path}",
            *self._extra_args,
            stdout=asyncio.subprocess.DEVNULL,
            # Piped (not DEVNULL) and drained by _log_stderr for the life of
            # the process - both so a startup failure (e.g. mpv can't open
            # its DRM/KMS output) is visible in `journalctl` instead of
            # silently discarded, and so the pipe never fills up and blocks
            # mpv if it warns about anything later during normal playback.
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._log_stderr())

        for _ in range(MPV_STARTUP_TIMEOUT_SECONDS * 10):
            if socket_path.exists():
                break
            if self._process.returncode is not None:
                raise RuntimeError(f"mpv exited immediately with code {self._process.returncode} - see logs above for its stderr")
            await asyncio.sleep(0.1)
        else:
            self._process.kill()
            raise RuntimeError("mpv did not create its IPC socket in time - see logs above for its stderr")

        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        self._listen_task = asyncio.create_task(self._listen())
        for name, value in PLAYBACK_TUNING.items():
            await self._command("set_property", name, value)

    async def _log_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.warning("mpv: %s", line.decode(errors="replace").rstrip())

    async def stop_process(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._writer:
            self._writer.close()
        if self._process and self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()

    async def _listen(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("event") == "file-loaded" or (
                message.get("event") == "end-file" and message.get("reason") in ("eof", "error")
            ):
                self._loading = False
            if message.get("event") == "end-file" and message.get("reason") == "error":
                self.media_failed = True
                self.failed = True
            if message.get("event") == "end-file" and message.get("reason") == "eof":
                self.finished = True
            request_id = message.get("request_id")
            if request_id is not None and request_id in self._pending:
                self._pending.pop(request_id).set_result(message)

    async def _command(self, *args) -> dict:
        assert self._writer is not None
        request_id = next(self._request_id)
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        self._writer.write(payload.encode("utf-8"))
        await self._writer.drain()
        try:
            response = await asyncio.wait_for(future, timeout=5)
        finally:
            self._pending.pop(request_id, None)
        # mpv reports command failures (bad syntax, wrong arg types, etc.) in
        # this same success-path response rather than as a socket-level
        # error, so nothing here would otherwise surface one - a bad
        # loadfile call once silently did nothing for exactly this reason.
        if response.get("error") != "success":
            logger.warning("mpv command %r failed: %s", args, response.get("error"))
        return response

    async def load(self, path: Path, *, start_seconds: int = 0, paused: bool = False) -> None:
        # pause=no is passed as part of loadfile's own options rather than as
        # a separate play() call afterward: loadfile acknowledges near-
        # instantly, but actually opening/probing the file happens
        # asynchronously and can take real time for a large file on this
        # hardware (same reason PROBE_TIMEOUT_SECONDS in library.py had to
        # grow) - a separate pause=no sent right after often landed before
        # that finished, and got silently reset to paused once mpv's own
        # load transition completed. Setting it as a loadfile option applies
        # atomically as part of the same load, so there's no race to lose.
        # start=<seconds> rides along in the same options string for exactly
        # the same reason - resuming a persisted position (see
        # PlaybackController.restore_last_playback) needs to land in the same
        # atomic step, not a separate seek() afterward that could just as
        # easily lose the same race.
        #
        # mpv's loadfile signature is <url> [<flags> [<index> [<options>]]] -
        # options is the 4th positional, not the 3rd; passing it 3rd (as an
        # earlier version of this fix did) makes mpv try to parse it as the
        # integer <index> and reject the whole command with "invalid
        # parameter", silently. index is irrelevant for "replace" but must
        # still be passed positionally to reach options.
        self.media_failed = False
        self.failed = False
        self.finished = False
        self._loading = True
        options = f"start={start_seconds},pause={'yes' if paused else 'no'}"
        response = await self._command("loadfile", str(path), "replace", 0, options)
        if response.get("error") != "success":
            self.failed = True
            self._loading = False

    async def show_image(self, path: Path) -> None:
        """Like load(), but for a still image meant to sit on screen
        indefinitely (the idle/home screen) rather than play through and
        stop - mpv's default image-display-duration is finite, which would
        otherwise drop it back to idle almost immediately. Options is the
        4th positional arg (see load()'s comment) - index 0 in between is
        required to reach it, not optional."""
        self.media_failed = False
        self.failed = False
        self.finished = False
        await self._command("loadfile", str(path), "replace", 0, "image-display-duration=inf")

    async def play(self) -> None:
        await self._command("set_property", "pause", False)

    async def pause(self) -> None:
        await self._command("set_property", "pause", True)

    async def stop(self) -> None:
        self.media_failed = False
        self.failed = False
        self.finished = False
        await self._command("stop")

    async def seek(self, position_seconds: int) -> None:
        await self._command("seek", position_seconds, "absolute")

    async def get_position(self) -> int:
        try:
            response = await self._command("get_property", "time-pos")
            return int(response.get("data") or 0)
        except (asyncio.TimeoutError, TypeError):
            return 0

    async def get_paused(self) -> bool:
        try:
            response = await self._command("get_property", "pause")
            return bool(response.get("data"))
        except asyncio.TimeoutError:
            return True

    async def get_idle(self) -> bool:
        # A load is acknowledged before probing finishes; it is not an idle movie.
        if self._loading:
            return False
        try:
            response = await self._command("get_property", "idle-active")
            return bool(response.get("data"))
        except asyncio.TimeoutError:
            return True

    async def set_dim(self, percent: int) -> None:
        """Simulates dimming via mpv's brightness video-equalizer property
        (-100..100, 0 = normal, negative = darker) - the Pi's HDMI output
        has no backlight control accessible from software, so this is the
        most direct lever mpv exposes for "make the picture darker" without
        actually stopping video output. Pass 0 to undo."""
        await self._command("set_property", "brightness", -abs(percent))

    async def show_pause_icon(self) -> None:
        """Persistent pause glyph overlaid near the bottom-left corner -
        stays up until hide_pause_icon() clears it, unlike show-text's
        timed messages."""
        await self._command(
            "osd-overlay", _PAUSE_ICON_OVERLAY_ID, "ass-events", _PAUSE_ICON_ASS, _OSD_RES_X, _OSD_RES_Y,
        )

    async def hide_pause_icon(self) -> None:
        await self._command(
            "osd-overlay", _PAUSE_ICON_OVERLAY_ID, "none", "", _OSD_RES_X, _OSD_RES_Y,
        )
