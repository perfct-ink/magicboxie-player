import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from player_app.models.library import MovieLibrary
from player_app.services import transcode_service
from player_app.services.transcode_service import TranscodeService


def _controller(tmp_path, **overrides):
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    library = MovieLibrary(movies_dir, thumbnail_dir=tmp_path / "thumbnails", transcode_dir=tmp_path / "transcoded")
    controller = SimpleNamespace(library=library, is_idle=True, sync_busy=False,
                                 currently_transcoding_movie_id=None, transcode_position_seconds=None,
                                 transcode_paused=False)
    controller.__dict__.update(overrides)
    return controller


def _refresh(controller):
    controller.library.scan(fast=True)
    controller.movies = controller.library.movies


def test_runs_only_while_idle_not_downloading_and_cool(tmp_path):
    temperature = [50.0]
    controller = _controller(tmp_path)
    service = TranscodeService(controller, temperature=lambda: temperature[0])
    assert service.may_run()
    controller.is_idle = False
    assert not service.may_run()
    controller.is_idle, controller.sync_busy = True, True
    assert not service.may_run()
    controller.sync_busy = False
    temperature[0] = transcode_service.PAUSE_AT_CELSIUS
    assert not service.may_run()
    # Waits until it has properly cooled, not just dipped under the limit.
    temperature[0] = transcode_service.PAUSE_AT_CELSIUS - 1
    assert not service.may_run()
    temperature[0] = transcode_service.RESUME_BELOW_CELSIUS - 1
    assert service.may_run()
    temperature[0] = None
    assert service.may_run()


def test_skips_movies_that_are_already_the_servers_player_copy(tmp_path):
    controller = _controller(tmp_path)
    (controller.library.root / "Copy.mp4").write_bytes(b"x")
    (controller.library.root / "Original.mkv").write_bytes(b"x")
    _refresh(controller)
    copy = next(m for m in controller.movies if m.title == "Copy")
    controller.library.save_metadata(copy.id, player_copy_version=2)
    service = TranscodeService(controller, temperature=lambda: None)
    assert service._next_movie().title == "Original"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_transcodes_to_fit_the_player_screen(tmp_path):
    controller = _controller(tmp_path)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x800:r=24",
                    "-f", "lavfi", "-i", "sine", "-t", "1", str(controller.library.root / "Moana.mkv")], check=True)
    _refresh(controller)
    movie = controller.movies[0]
    service = TranscodeService(controller, temperature=lambda: None)

    asyncio.run(service._transcode(movie, asyncio.Event()))

    out = controller.library.playable_path_for(movie.id)
    assert out == controller.library.transcode_path_for(movie.id)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,profile", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "Constrained Baseline,720,300"
    assert controller.currently_transcoding_movie_id is None
