"""Fakes for testing PlaybackController/transports without real mpv or BlueZ."""
from pathlib import Path

from player_app.models.protocol import Movie


class FakeMpv:
    def __init__(self):
        self.loading = False
        self.media_failed = False
        self.failed = False
        self.finished = False
        self.loaded_path = None
        self.shown_image_path = None
        self.paused = True
        self.idle = True
        self.position = 0
        self.dim_percent = 0
        self.pause_icon_shown = False

    async def load(self, path, *, start_seconds=0, paused=False):
        self.media_failed = False
        self.failed = False
        self.finished = False
        self.loaded_path = path
        self.idle = False
        self.paused = paused
        self.position = start_seconds

    async def show_image(self, path):
        self.media_failed = False
        self.failed = False
        self.finished = False
        self.shown_image_path = path
        self.idle = False
        self.paused = True

    async def play(self):
        self.paused = False

    async def pause(self):
        self.paused = True

    async def stop(self):
        self.media_failed = False
        self.failed = False
        self.finished = False
        self.idle = True
        self.paused = True
        self.position = 0

    async def seek(self, position_seconds):
        self.position = position_seconds

    async def get_position(self):
        return self.position

    async def get_paused(self):
        return self.paused

    async def get_idle(self):
        return self.idle

    async def set_dim(self, percent):
        self.dim_percent = percent

    async def show_pause_icon(self):
        self.pause_icon_shown = True

    async def hide_pause_icon(self):
        self.pause_icon_shown = False


class FakeLibrary:
    def __init__(self, thumbnail_paths=None, thumbnail_dir=None, metadata=None, transcode_dir=None):
        self._paths = {0: Path("/movies/a.mp4"), 1: Path("/movies/b.mp4")}
        self._thumbnail_paths = thumbnail_paths or {}
        self._thumbnail_dir = thumbnail_dir
        self._metadata = metadata or {}
        # Real dir (mirrors MovieLibrary) so tests can create/omit a file
        # there to control transcode_path_for(...).exists() naturally,
        # rather than a separate bookkeeping dict that could drift from the
        # real disk-existence-based behavior it's standing in for.
        self._transcode_dir = transcode_dir or Path("/transcoded")

    @property
    def movies(self):
        return [
            Movie(id=0, title="A", duration_seconds=100),
            Movie(id=1, title="B", duration_seconds=200),
        ]

    def path_for(self, movie_id):
        return self._paths[movie_id]

    def transcode_path_for(self, movie_id):
        return self._transcode_dir / f"{movie_id}.mp4"

    def playable_path_for(self, movie_id):
        transcoded = self.transcode_path_for(movie_id)
        return transcoded if transcoded.exists() else self._paths[movie_id]

    def needs_transcoding(self, movie_id):
        if self._metadata.get(movie_id, {}).get("player_copy_version"):
            return False
        return not self.transcode_path_for(movie_id).exists()

    def quarantine_failed_playback(self, movie_id):
        return False

    def thumbnail_path_for(self, movie_id):
        return self._thumbnail_paths.get(movie_id)

    def delete(self, movie_id):
        self._paths.pop(movie_id, None)

    def save_uploaded_thumbnail(self, movie_id, data):
        if self._thumbnail_dir is None:
            raise RuntimeError("FakeLibrary needs thumbnail_dir to save uploads")
        path = self._thumbnail_dir / f"{movie_id}.jpg"
        path.write_bytes(data)
        self._thumbnail_paths[movie_id] = path

    def metadata_for(self, movie_id):
        return self._metadata.get(movie_id, {})

    def save_metadata(self, movie_id, **fields):
        updated = {**self._metadata.get(movie_id, {}), **fields}
        self._metadata[movie_id] = updated
        return updated
