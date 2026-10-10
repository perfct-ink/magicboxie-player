"""Scans a directory of video files and exposes them via protocol.Movie."""
from __future__ import annotations

import logging
import os
import subprocess
import zlib
import threading
import time

from PIL import Image

from ..storage import atomic_write, publish_file, read_dict, write_json
from pathlib import Path
from typing import Dict, List, Optional

from .protocol import Movie

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v"}

DEFAULT_THUMBNAIL_DIR = Path("/var/lib/magicboxie/thumbnails")
DEFAULT_TRANSCODE_DIR = Path("/var/lib/magicboxie/transcoded")
THUMBNAIL_TIMESTAMP_SECONDS = 60
THUMBNAIL_WIDTH = 200

# How long to wait for ffprobe to report a file's duration. Was 10s, which
# sounds generous but isn't on a Pi Zero W: probing a real, multi-hundred-MB
# movie (as opposed to the tiny synthetic clips used in dev) measured ~19s
# there, so every real movie was silently timing out and getting recorded
# with duration=0. A one-shot per-movie cost at scan time, not per playback,
# so there's no reason to keep this tight.
PROBE_TIMEOUT_SECONDS = 60

# Movie ids must stay under this - protocol.encode_status packs movie_id into
# 2 bytes with 0xFFFF reserved to mean "no movie" (see protocol.py's
# _NO_MOVIE_SENTINEL), so valid ids run 0..0xFFFE.
_MOVIE_ID_SPACE = 0xFFFF


class MovieLibrary:
    def __init__(
        self,
        root: Path,
        thumbnail_dir: Path = DEFAULT_THUMBNAIL_DIR,
        transcode_dir: Path = DEFAULT_TRANSCODE_DIR,
    ):
        # The movies directory is typically mounted read-only, so thumbnails
        # (and phone-supplied metadata) are cached in a separate, writable
        # location instead of alongside it.
        self._scan_lock = threading.RLock()
        self.root = root
        self._movies: List[Movie] = []
        self._paths: Dict[int, Path] = {}
        self._thumbnail_paths: Dict[int, Path] = {}
        self._metadata: Dict[int, dict] = {}
        self._thumbnail_dir = thumbnail_dir
        self._transcode_dir = transcode_dir
        self._id_map_path = thumbnail_dir / "movie_ids.json"
        # filename -> id, persisted across restarts (see _stable_id's own
        # docstring for why: computing a fresh hash every scan is only
        # actually stable id-to-filename in the no-collision case - once a
        # collision happens, which of the colliding files gets bumped to
        # hash+1 depends on which one this scan happens to reach first, and
        # that can change if the file *set* changes (one added, one
        # removed) even though the specific file in question was never
        # touched. Recording the assignment the first time a filename is
        # ever seen and always reusing it after makes an id permanent for
        # that filename, full stop - not just usually stable.
        self._id_by_filename: Dict[str, int] = {}

    def scan(self, *, fast: bool = False) -> List[Movie]:
        with self._scan_lock:
            return self._scan(fast=fast)

    def cleanup_partial_files(self) -> None:
        """Only called before workers start; never remove an active download."""
        for directory in (self.root, self._thumbnail_dir, self._transcode_dir):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.is_file() and (path.name.endswith(".partial") or ".partial." in path.name):
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Cannot remove interrupted write %s", path)

    def _scan(self, *, fast: bool) -> List[Movie]:
        """Reads the movies directory from the filesystem and caches the
        result (title, duration, thumbnail, metadata) in memory for fast
        lookups."""
        self._thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self._id_by_filename = self._load_id_map()
        cache_path = self._thumbnail_dir / "scan_cache.json"
        cache = read_dict(cache_path)
        updated_cache = {}

        files = sorted(
            p for p in self.root.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and ".partial." not in p.name and not p.name.startswith(".")
        )

        movies = []
        paths = {}
        thumbnail_paths = {}
        metadata = {}
        taken_ids = set(self._id_by_filename.values())
        for path in files:
            if path.name in self._id_by_filename:
                movie_id = self._id_by_filename[path.name]
            else:
                movie_id = self._stable_id(path, taken=taken_ids)
                self._id_by_filename[path.name] = movie_id
                taken_ids.add(movie_id)

            stat = path.stat()
            signature = [stat.st_size, stat.st_mtime_ns]
            cached = cache.get(path.name, {})
            valid_cache = (isinstance(cached, dict) and cached.get("signature") == signature
                           and type(cached.get("duration")) is int and cached["duration"] > 0)
            if stat.st_size == 0:
                logger.warning("Quarantining empty movie %s", path)
                try:
                    path.rename(path.with_name(f"{path.name}.{time.time_ns()}.corrupt"))
                except OSError:
                    logger.warning("Could not quarantine empty movie %s", path)
                continue
            duration = cached["duration"] if valid_cache else (0 if fast else self._probe_duration(path))
            if duration > 0:
                updated_cache[path.name] = {"signature": signature, "duration": duration}
            movies.append(Movie(id=movie_id, title=path.stem, duration_seconds=duration))
            paths[movie_id] = path

            thumbnail = self._thumbnail_dir / f"{movie_id}.jpg"
            if not self._valid_thumbnail(thumbnail):
                thumbnail = None if fast else self._ensure_thumbnail(movie_id, path)
            if thumbnail is not None:
                thumbnail_paths[movie_id] = thumbnail

            metadata[movie_id] = self._load_metadata(movie_id)

        self._movies = movies
        self._paths = paths
        self._thumbnail_paths = thumbnail_paths
        self._metadata = metadata
        self._save_id_map()
        write_json(cache_path, updated_cache)
        logger.info("Scanned %d movie(s) in %s", len(movies), self.root)
        return movies

    def _load_id_map(self) -> Dict[str, int]:
        raw = read_dict(self._id_map_path)
        result = {}
        taken = set()
        for name, value in raw.items():
            if isinstance(name, str) and type(value) is int and 0 <= value < _MOVIE_ID_SPACE and value not in taken:
                result[name] = value
                taken.add(value)
        return result

    def _save_id_map(self) -> None:
        try:
            write_json(self._id_map_path, self._id_by_filename)
        except OSError:
            logger.warning("Failed to persist %s - ids may not survive a restart", self._id_map_path)

    @property
    def movies(self) -> List[Movie]:
        return self._movies

    def path_for(self, movie_id: int) -> Path:
        return self._paths[movie_id]

    def transcode_path_for(self, movie_id: int) -> Path:
        """Where a version of this movie re-encoded on this device (by an
        older release, which transcoded here) lives, if one still does -
        see playable_path_for(). Nothing writes new ones any more."""
        return self._transcode_dir / f"{movie_id}.mp4"

    def playable_path_for(self, movie_id: int) -> Path:
        """What to actually hand mpv: a copy transcoded here by an older
        release if one is left over, otherwise the movie's own file."""
        transcoded = self.transcode_path_for(movie_id)
        return transcoded if transcoded.exists() else self._paths[movie_id]

    def needs_transcoding(self, movie_id: int) -> bool:
        """Whether this movie still needs re-encoding for this device: its
        file isn't the media server's player copy (metadata
        player_copy_version, see home_sync_service.py) and no copy has been
        transcoded here yet."""
        if self._metadata.get(movie_id, {}).get("player_copy_version"):
            return False
        return not self.transcode_path_for(movie_id).exists()

    def replace_file(self, movie_id: int, downloaded: Path, filename: str) -> None:
        """Swaps a movie's file for a newly downloaded one (the home server's
        current player copy), keeping its id - and so its thumbnail, metadata
        and resume position. The old file goes, whatever its name, along with
        any copy an older release transcoded here (which playable_path_for
        would otherwise keep preferring)."""
        with self._scan_lock:
            old = self._paths.get(movie_id)
            target = self.root / filename
            publish_file(downloaded, target)
            if old is not None and old != target:
                old.unlink(missing_ok=True)
                self._id_by_filename.pop(old.name, None)
            self._id_by_filename[target.name] = movie_id
            self._save_id_map()
            self._paths[movie_id] = target
            self.transcode_path_for(movie_id).unlink(missing_ok=True)
            logger.info("Replaced movie %d's file with %s", movie_id, target.name)

    def quarantine_failed_playback(self, movie_id: int) -> bool:
        """Preserve rejected media outside the library; retry originals after a bad encode."""
        with self._scan_lock:
            path = self.playable_path_for(movie_id)
            optimized = path == self.transcode_path_for(movie_id)
            try:
                path.rename(path.with_name(f"{path.name}.{time.time_ns()}.corrupt"))
            except OSError:
                logger.warning("Could not quarantine %s", path)
                return False
            if not optimized:
                self._movies = [movie for movie in self._movies if movie.id != movie_id]
                self._paths.pop(movie_id, None)
            return optimized

    def thumbnail_path_for(self, movie_id: int) -> Optional[Path]:
        return self._thumbnail_paths.get(movie_id)

    def save_uploaded_thumbnail(self, movie_id: int, data: bytes) -> None:
        """Overwrites the cached thumbnail with a phone-supplied "official" one
        (e.g. fetched from TMDB), so future requests - from any device - get
        it instead of the local ffmpeg frame grab."""
        self._thumbnail_dir.mkdir(parents=True, exist_ok=True)
        thumbnail_path = self._thumbnail_dir / f"{movie_id}.jpg"
        atomic_write(thumbnail_path, data)
        self._thumbnail_paths[movie_id] = thumbnail_path

    def metadata_for(self, movie_id: int) -> dict:
        return self._metadata.get(movie_id, {})

    def delete(self, movie_id: int) -> None:
        """Permanently removes a movie: its source file, any transcoded
        copy, thumbnail, and metadata, then rescans so in-memory state (and
        the persisted id map) reflects the removal immediately rather than
        waiting for whatever triggers the next scan. Raises KeyError for an
        unknown id - callers (see web_service._delete_movie) check
        existence themselves first for a clean 404 instead."""
        path = self._paths[movie_id]

        path.unlink(missing_ok=True)
        self.transcode_path_for(movie_id).unlink(missing_ok=True)
        thumbnail = self._thumbnail_paths.get(movie_id)
        if thumbnail is not None:
            thumbnail.unlink(missing_ok=True)
        (self._thumbnail_dir / f"{movie_id}.json").unlink(missing_ok=True)
        # scan() reloads _id_by_filename from disk as its first step, so the
        # removal has to be saved before calling it or scan() would just
        # read the stale (pre-removal) map straight back in.
        self._id_by_filename.pop(path.name, None)
        self._save_id_map()

        self.scan()

    def save_metadata(self, movie_id: int, **fields) -> dict:
        """Merges phone-supplied metadata (e.g. title/description/year fetched
        online) into what's cached for this movie and persists it to disk
        alongside the thumbnail, so it survives a rescan/restart and is
        available to any device that connects."""
        self._thumbnail_dir.mkdir(parents=True, exist_ok=True)
        updated = {**self._metadata.get(movie_id, {}), **fields}
        self._metadata[movie_id] = updated
        metadata_path = self._thumbnail_dir / f"{movie_id}.json"
        write_json(metadata_path, updated)
        return updated

    @staticmethod
    def _stable_id(path: Path, taken: set) -> int:
        """Computes an id for a filename the persisted map (see
        _id_by_filename) has never seen before - only ever called once per
        filename, the first time it's encountered, since scan() reuses the
        persisted id on every scan after that. The previous scheme (a plain
        enumerate() index over the sorted file list) shifted for every movie
        whenever a file was added or removed anywhere earlier in sort order,
        silently reattaching one movie's thumbnail/metadata/selection to
        whatever movie now landed on its old id.

        taken is every id already assigned to some other filename (from the
        persisted map, plus anything assigned earlier in this same scan),
        used to probe past a hash collision (rare for a personal-library-
        sized directory, but cheap to handle) rather than letting two files
        share an id.
        """
        movie_id = zlib.crc32(path.name.encode("utf-8")) % _MOVIE_ID_SPACE
        if len(taken) >= _MOVIE_ID_SPACE:
            raise ValueError("Movie ID space exhausted")
        while movie_id in taken:
            movie_id = (movie_id + 1) % _MOVIE_ID_SPACE
        return movie_id

    def _load_metadata(self, movie_id: int) -> dict:
        metadata_path = self._thumbnail_dir / f"{movie_id}.json"
        return read_dict(metadata_path)

    @staticmethod
    def _valid_thumbnail(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _probe_duration(path: Path) -> int:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=True,
            )
            return int(float(result.stdout.strip()))
        except (subprocess.SubprocessError, ValueError, OverflowError, OSError) as exc:
            logger.warning("ffprobe failed for %s: %s", path, exc)
            return 0

    def _ensure_thumbnail(self, movie_id: int, path: Path) -> Optional[Path]:
        thumbnail_path = self._thumbnail_dir / f"{movie_id}.jpg"
        if self._valid_thumbnail(thumbnail_path):
            return thumbnail_path
        temporary = thumbnail_path.with_suffix(".partial.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-ss", str(THUMBNAIL_TIMESTAMP_SECONDS),
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", f"scale={THUMBNAIL_WIDTH}:-1",
                    str(temporary),
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            if not self._valid_thumbnail(temporary):
                return None
            publish_file(temporary, thumbnail_path)
            return thumbnail_path
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Thumbnail generation failed for %s: %s", path, exc)
            return None
        finally:
            temporary.unlink(missing_ok=True)
