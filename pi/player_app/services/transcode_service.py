"""Background transcoding on the player, for movies whose file isn't the
media server's player copy (downloaded as the full-size original, copied on
by hand, or a copy made before the server's current encoding): re-encodes
them, one at a time, to what the server makes - fitted inside 720x480,
progressive, H.264 Baseline - so the Pi Zero plays them smoothly.

Only runs while nothing plays, nothing downloads and the device is cool
enough. When any of that changes mid-encode, ffmpeg is paused (SIGSTOP)
rather than killed, so a long movie's progress isn't thrown away; it picks
up again once the device is idle and cool."""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Callable, Optional

from ..controllers.playback_controller import PlaybackController
from ..models.protocol import Movie
from ..storage import publish_file, run_io
from ..util import cpu_temperature_celsius, sleep_unless_stopped

logger = logging.getLogger(__name__)

# The media server's PlayerFFmpegArgs (magicboxie-mediaserver), with
# ultrafast so the encode itself doesn't take forever on this CPU - at the
# cost of a bigger file.
FFMPEG_ENCODE_ARGS = [
    "-vf", "bwdif=deint=interlaced,scale=w='min(720,iw)':h='min(480,ih)'"
           ":force_original_aspect_ratio=decrease:force_divisible_by=2",
    "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
    "-preset", "ultrafast", "-crf", "23",
    # Baseline is 8-bit 4:2:0 only (10-bit sources otherwise fail), and the
    # car has stereo speakers (5.1 otherwise fails the aac encoder).
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-ac", "2", "-b:a", "128k",
    "-movflags", "+faststart",
]

# Transcoding stops at this temperature and only starts again once the
# device has cooled to RESUME_BELOW_CELSIUS - well under ThermalService's
# 80°C, where playback itself gets paused.
PAUSE_AT_CELSIUS = 70.0
RESUME_BELOW_CELSIUS = 65.0

# How often a running encode checks whether it should pause or resume.
CHECK_INTERVAL_SECONDS = 1.0
# How long to wait before looking again when there's nothing to do.
IDLE_POLL_INTERVAL_SECONDS = 15.0

class TranscodeService:
    def __init__(self, controller: PlaybackController,
                 temperature: Callable[[], Optional[float]] = cpu_temperature_celsius):
        self._controller = controller
        self._temperature = temperature
        self._too_hot = False
        # Movies ffmpeg couldn't encode, skipped until restart rather than
        # retried forever.
        self._failed_movie_ids: set = set()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            movie = self._next_movie() if self.may_run() else None
            if movie is None:
                await sleep_unless_stopped(stop_event, IDLE_POLL_INTERVAL_SECONDS)
                continue
            try:
                await self._transcode(movie, stop_event)
            except Exception:
                logger.exception("Transcoding movie %d failed unexpectedly", movie.id)
                self._failed_movie_ids.add(movie.id)

    def may_run(self) -> bool:
        """Idle (nothing playing or downloading) and not too hot. A missing
        temperature reading doesn't stop it."""
        temperature = self._temperature()
        if temperature is not None:
            if temperature >= PAUSE_AT_CELSIUS:
                if not self._too_hot:
                    logger.info("Transcoding waits: device at %.0f°C", temperature)
                self._too_hot = True
            elif temperature < RESUME_BELOW_CELSIUS:
                self._too_hot = False
        return self._controller.is_idle and not self._controller.sync_busy and not self._too_hot

    def _next_movie(self) -> Optional[Movie]:
        library = self._controller.library
        for movie in self._controller.movies:
            if movie.id not in self._failed_movie_ids and library.needs_transcoding(movie.id):
                return movie
        return None

    async def _transcode(self, movie: Movie, stop_event: asyncio.Event) -> None:
        library = self._controller.library
        source = library.path_for(movie.id)
        dest = library.transcode_path_for(movie.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".partial" + dest.suffix)

        controller = self._controller
        controller.currently_transcoding_movie_id = movie.id
        controller.transcode_position_seconds = 0.0
        logger.info("Transcoding %s", source.name)
        process = await asyncio.create_subprocess_exec(
            "nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", str(source), *FFMPEG_ENCODE_ARGS, str(tmp_dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        progress = asyncio.create_task(self._read_progress(process.stdout))
        stderr = asyncio.create_task(process.stderr.read())
        paused = False
        try:
            while process.returncode is None:
                if stop_event.is_set():
                    return
                should_run = self.may_run()
                if paused != (not should_run):
                    paused = not should_run
                    logger.info("%s transcode of %s", "Pausing" if paused else "Resuming", source.name)
                    process.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
                controller.transcode_paused = paused
                try:
                    await asyncio.wait_for(process.wait(), timeout=CHECK_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    continue

            if process.returncode == 0:
                await run_io(publish_file, tmp_dest, dest)
                logger.info("Finished transcoding %s", source.name)
                return
            logger.warning("Transcoding %s failed (exit %d): %s", source.name, process.returncode,
                           (await stderr).decode(errors="replace"))
            self._failed_movie_ids.add(movie.id)
        finally:
            if process.returncode is None:
                process.send_signal(signal.SIGCONT)
                process.kill()
                await process.wait()
            for task in (progress, stderr):
                task.cancel()
            await asyncio.gather(progress, stderr, return_exceptions=True)
            tmp_dest.unlink(missing_ok=True)
            controller.currently_transcoding_movie_id = None
            controller.transcode_position_seconds = None
            controller.transcode_paused = False

    async def _read_progress(self, stdout) -> None:
        """ffmpeg's -progress output is key=value lines; out_time_us is how
        far into the movie the encode has got, for the activity card."""
        if stdout is None:
            return
        while True:
            line = await stdout.readline()
            if not line:
                return
            key, _, value = line.decode(errors="replace").strip().partition("=")
            # Older ffmpeg names it out_time_ms, but it is microseconds too.
            if key in ("out_time_us", "out_time_ms") and value.isdigit():
                self._controller.transcode_position_seconds = int(value) / 1_000_000
