import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from player_app.models.library import MovieLibrary
from player_app.storage import atomic_write
from player_app import update_status


def library_at(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    return MovieLibrary(root, thumbnail_dir=tmp_path / "thumbs", transcode_dir=tmp_path / "encoded")


def test_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old")
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("power lost")))
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_fast_scan_never_probes_and_cached_scan_reuses_duration(tmp_path, monkeypatch):
    library = library_at(tmp_path)
    movie = library.root / "movie.mp4"
    movie.write_bytes(b"movie")
    probe = Mock(return_value=123)
    monkeypatch.setattr(library, "_probe_duration", probe)
    monkeypatch.setattr(library, "_ensure_thumbnail", Mock(return_value=None))
    library.scan(fast=True)
    probe.assert_not_called()
    library.scan()
    assert probe.call_count == 1
    library.scan()
    assert probe.call_count == 1
    movie.write_bytes(b"changed movie")
    library.scan()
    assert probe.call_count == 2


@pytest.mark.parametrize("contents", ['[1, 2]', '{"a.mp4": [], "b.mp4": -1}', '{bad', '{"a.mp4": 1, "b.mp4": 1}'])
def test_corrupt_id_cache_cannot_break_startup(tmp_path, contents):
    library = library_at(tmp_path)
    library._thumbnail_dir.mkdir()
    library._id_map_path.write_text(contents)
    for name in ("a.mp4", "b.mp4"):
        (library.root / name).write_bytes(b"movie")
    library.scan(fast=True)
    assert len({movie.id for movie in library.movies}) == 2


def test_startup_removes_partial_files_and_rejects_bad_thumbnails(tmp_path):
    library = library_at(tmp_path)
    (library.root / ".movie.mp4.partial").write_bytes(b"incomplete")
    (library.root / "movie.mp4").write_bytes(b"movie")
    library.cleanup_partial_files()
    library.scan(fast=True)
    movie_id = library.movies[0].id
    (library._thumbnail_dir / f"{movie_id}.jpg").write_bytes(b"broken")
    library.scan(fast=True)
    assert library.thumbnail_path_for(movie_id) is None
    assert not (library.root / ".movie.mp4.partial").exists()


def test_quarantine_bad_encode_retries_original_then_excludes_bad_original(tmp_path):
    library = library_at(tmp_path)
    (library.root / "movie.mp4").write_bytes(b"movie")
    library.scan(fast=True)
    movie_id = library.movies[0].id
    encoded = library.transcode_path_for(movie_id)
    encoded.parent.mkdir()
    encoded.write_bytes(b"broken")
    assert library.quarantine_failed_playback(movie_id)
    assert library.playable_path_for(movie_id) == library.root / "movie.mp4"
    assert not library.quarantine_failed_playback(movie_id)
    library.scan(fast=True)
    assert library.movies == []


def test_stale_update_marker_is_ignored(tmp_path, monkeypatch):
    marker = tmp_path / "update.json"
    monkeypatch.setattr(update_status, "STATUS_PATH", marker)
    marker.write_text(json.dumps({"phase": "installing", "pid": os.getpid(), "at": 0}))
    assert update_status.read_status() is None


def test_interrupted_download_never_publishes_movie(tmp_path):
    import asyncio
    from player_app.services.home_sync_service import HomeServerSync

    library = library_at(tmp_path)
    sync = HomeServerSync(library, "http://example.test", "password")

    class Content:
        async def iter_chunked(self, size):
            yield b"partial movie"
            assert not (library.root / "Movie.mp4").exists()
            library.scan(fast=True)
            assert library.movies == []
            raise asyncio.CancelledError()

    class Response:
        status = 200
        content_length = None
        content = Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sync._download_movie(Session(), {"Id": "1", "Name": "Movie"}, None))
    assert list(library.root.iterdir()) == []
