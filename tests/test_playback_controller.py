import asyncio
import json
from unittest.mock import AsyncMock, patch

from fakes import FakeLibrary, FakeMpv

from player_app.controllers.playback_controller import PAUSE_DIM_PERCENT, PlaybackController
from player_app.models.protocol import Command, Opcode, PlaybackStatus


def test_select_and_play_updates_status():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=1))
        return await controller.refresh_status()

    state = asyncio.run(scenario())
    assert state.status == PlaybackStatus.PLAYING
    assert state.movie_id == 1


def test_select_unknown_movie_id_is_ignored_not_a_crash():
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=99999))
        return controller, mpv

    controller, mpv = asyncio.run(scenario())
    assert controller.is_idle
    assert mpv.loaded_path is None


def test_stop_clears_movie_id():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.STOP))
        return await controller.refresh_status()

    state = asyncio.run(scenario())
    assert state.status == PlaybackStatus.STOPPED
    assert state.movie_id is None


def test_stop_shows_idle_screen():
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.STOP))
        return mpv

    mpv = asyncio.run(scenario())
    assert mpv.shown_image_path is not None


def test_status_stays_idle_while_idle_screen_is_shown():
    """The idle-screen image is itself loaded into mpv as a "file" (see
    MpvController.show_image), which would otherwise report as "not idle" -
    refresh_status must not mistake it for a movie playing."""
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.show_idle_screen()
        return await controller.refresh_status()

    state = asyncio.run(scenario())
    assert state.status == PlaybackStatus.STOPPED
    assert state.movie_id is None


def test_slideshow_steps_through_movies_only_while_idle(tmp_path):
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(_transcoded_library(tmp_path), mpv)
        seen = []
        await controller.advance_slideshow()
        seen.append(controller.slide_index)
        await controller.advance_slideshow()
        seen.append(controller.slide_index)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.advance_slideshow()
        seen.append(controller.slide_index)
        return seen, mpv

    seen, mpv = asyncio.run(scenario())
    # Two movies: wraps back to the first, then holds while one plays.
    assert seen == [1, 0, 0]
    assert mpv.shown_image_path is not None


def test_pause_reflected_in_status():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.PAUSE))
        return await controller.refresh_status()

    state = asyncio.run(scenario())
    assert state.status == PlaybackStatus.PAUSED


def test_seek_updates_position():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.SEEK, argument=42))
        return await controller.refresh_status()

    state = asyncio.run(scenario())
    assert state.position_seconds == 42


def test_pause_dims_and_shows_pause_icon():
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.PAUSE))
        return mpv

    mpv = asyncio.run(scenario())
    assert mpv.dim_percent == PAUSE_DIM_PERCENT
    assert mpv.pause_icon_shown


def test_play_clears_dim_and_pause_icon():
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.PAUSE))
        await controller.handle_command(Command(opcode=Opcode.PLAY))
        return mpv

    mpv = asyncio.run(scenario())
    assert mpv.dim_percent == 0
    assert not mpv.pause_icon_shown


def test_stop_clears_dim_and_pause_icon_left_over_from_a_pause():
    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.handle_command(Command(opcode=Opcode.PAUSE))
        await controller.handle_command(Command(opcode=Opcode.STOP))
        return mpv

    mpv = asyncio.run(scenario())
    assert mpv.dim_percent == 0
    assert not mpv.pause_icon_shown


def test_any_command_updates_last_input_at():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        controller.last_input_at = 0
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        return controller.last_input_at

    last_input_at = asyncio.run(scenario())
    assert last_input_at > 0


def test_shutdown_invokes_systemctl_poweroff_via_sudo():
    # Patched out so this test never actually powers off the machine
    # running it.
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        with patch(
            "player_app.controllers.playback_controller.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as mock_exec:
            await controller.handle_command(Command(opcode=Opcode.SHUTDOWN))
            return mock_exec

    mock_exec = asyncio.run(scenario())
    mock_exec.assert_awaited_once_with("sudo", "-n", "/usr/bin/systemctl", "poweroff")


def test_refresh_status_persists_playback_state(tmp_path):
    state_path = tmp_path / "playback_state.json"

    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv, state_path=state_path)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=1))
        mpv.position = 42
        await controller.refresh_status()

    asyncio.run(scenario())

    assert json.loads(state_path.read_text()) == {
        "movie_id": 1,
        "position_seconds": 42,
        "paused": False,
    }


def test_stopped_movie_keeps_its_position_but_boot_shows_the_slideshow(tmp_path):
    state_path = tmp_path / "playback_state.json"
    player = FakeMpv()

    async def scenario():
        controller = PlaybackController(FakeLibrary(), player, state_path=state_path)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=1))
        player.position = 77
        await controller.handle_command(Command(opcode=Opcode.STOP))
        assert controller.is_idle
        restarted = PlaybackController(FakeLibrary(), FakeMpv(), state_path=state_path)
        assert not await restarted.restore_last_playback()
        # The slideshow's progress marker and a later select still use it.
        assert restarted.saved_position(1) == 77

    asyncio.run(scenario())

    assert json.loads(state_path.read_text()) == {
        "movie_id": 1, "position_seconds": 77, "paused": False, "stopped": True,
    }


def test_paused_movie_resumes_paused_on_its_last_frame_at_boot(tmp_path):
    state_path = tmp_path / "playback_state.json"
    player = FakeMpv()

    async def scenario():
        controller = PlaybackController(FakeLibrary(), player, state_path=state_path)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=1))
        await controller.handle_command(Command(opcode=Opcode.PAUSE))
        player.position = 55
        await controller.save_position_now()
        restarted_player = FakeMpv()
        restarted = PlaybackController(FakeLibrary(), restarted_player, state_path=state_path)
        assert await restarted.restore_last_playback()
        return restarted_player

    restarted_player = asyncio.run(scenario())
    assert restarted_player.paused
    assert restarted_player.position == 55


def _transcoded_library(tmp_path):
    """Both fake movies already transcoded: nothing left waiting."""
    for movie_id in (0, 1):
        (tmp_path / f"{movie_id}.mp4").touch()
    return FakeLibrary(transcode_dir=tmp_path)


def _activity(**fields):
    from player_app.services.home_sync_service import SyncActivity
    return SyncActivity(**fields)


def test_idle_activity_prefers_downloading_then_media_server(tmp_path):
    controller = PlaybackController(FakeLibrary(transcode_dir=tmp_path), FakeMpv())

    controller.sync_activity = _activity(reachable=True, preparing=[
        {"title": "Queued", "status": "pending", "progress_percent": None},
        {"title": "Zeta", "status": "transcoding", "progress_percent": 40.5},
    ])
    activity = controller.idle_activity
    assert (activity.heading, activity.title, activity.percent) == ("Transcoding", "Zeta", 40)
    assert activity.detail == "On the media server, 1 more waiting"

    controller.currently_syncing_movie_title = "Alpha"
    controller.sync_activity.bytes_done, controller.sync_activity.bytes_total = 30, 120
    controller.sync_activity.queued = ["Beta", "Gamma"]
    activity = controller.idle_activity
    assert (activity.heading, activity.title, activity.percent) == ("Downloading", "Alpha", 25)
    assert activity.detail == "2 more to download"

    controller.status_message = "Checking for updates\u2026"
    assert controller.idle_activity.heading == "Downloading"
    assert controller.activity_message == "Checking for updates\u2026"

    # Too hot outranks downloads, but not an update in progress.
    controller.thermal_note = "Too hot (82\u00b0C)."
    controller.status_message = None
    assert controller.activity_message == "Too hot (82\u00b0C)."

    # Installing an update outranks everything.
    controller.status_message = "Updating device software"
    assert controller.activity_message == "Updating device software"
    activity = controller.idle_activity
    assert (activity.heading, activity.title) == ("Updating", "Device software")


def test_waiting_downloads_keep_the_card_up_instead_of_the_slideshow(tmp_path):
    controller = PlaybackController(FakeLibrary(transcode_dir=tmp_path), FakeMpv())
    # Nothing local to wait for any more: the slideshow.
    assert controller.idle_activity is None

    controller.sync_activity = _activity(reachable=True, queued=["Alpha", "Beta"])
    activity = controller.idle_activity
    assert (activity.heading, activity.title) == ("Downloading", "Alpha")
    assert activity.detail == "Waiting to start, 1 more after it"


def test_media_server_status_only_shows_while_it_is_reachable(tmp_path):
    controller = PlaybackController(_transcoded_library(tmp_path), FakeMpv())
    controller.sync_activity = _activity(reachable=False, preparing=[
        {"title": "Zeta", "status": "transcoding", "progress_percent": 40.0},
    ])
    assert controller.idle_activity is None


def test_slideshow_holds_still_while_downloading(tmp_path):
    async def scenario():
        controller = PlaybackController(_transcoded_library(tmp_path), FakeMpv())
        with patch("player_app.controllers.playback_controller.render_idle_screen") as render:
            await controller.advance_slideshow()
            assert controller.slide_index == 1
            controller.currently_syncing_movie_title = "Alpha"
            await controller.advance_slideshow()
            assert controller.slide_index == 1
            kwargs = render.call_args.kwargs
            assert kwargs["activity"].title == "Alpha"
            assert kwargs["positions"] == {0: 0, 1: 0}

    asyncio.run(scenario())


def test_player_becoming_idle_without_eof_clears_persisted_state(tmp_path):
    state_path = tmp_path / "playback_state.json"

    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv, state_path=state_path)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.refresh_status()
        mpv.idle = True  # e.g. a failed load, rather than natural completion
        await controller.refresh_status()

    asyncio.run(scenario())

    assert not state_path.exists()


def test_no_persistence_without_a_state_path():
    """The default (no state_path passed) must not try to write anywhere -
    every other test in this file relies on that."""
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        await controller.refresh_status()
        return controller

    controller = asyncio.run(scenario())
    assert controller._state_path is None


def test_restore_last_playback_resumes_movie_and_position(tmp_path):
    state_path = tmp_path / "playback_state.json"
    state_path.write_text(json.dumps({"movie_id": 1, "position_seconds": 123, "paused": False}))

    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv, state_path=state_path)
        resumed = await controller.restore_last_playback()
        return controller, mpv, resumed

    controller, mpv, resumed = asyncio.run(scenario())
    assert resumed is True
    assert not controller.is_idle
    assert mpv.loaded_path == FakeLibrary()._paths[1]
    assert mpv.position == 123
    assert not mpv.paused


def test_restore_last_playback_resumes_paused_state(tmp_path):
    state_path = tmp_path / "playback_state.json"
    state_path.write_text(json.dumps({"movie_id": 0, "position_seconds": 10, "paused": True}))

    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv, state_path=state_path)
        await controller.restore_last_playback()
        return mpv

    mpv = asyncio.run(scenario())
    assert mpv.paused
    assert mpv.pause_icon_shown
    assert mpv.dim_percent == PAUSE_DIM_PERCENT


def test_restore_last_playback_returns_false_with_no_state_file(tmp_path):
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv(), state_path=tmp_path / "missing.json")
        return await controller.restore_last_playback()

    assert asyncio.run(scenario()) is False


def test_restore_last_playback_returns_false_without_a_state_path():
    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv())
        return await controller.restore_last_playback()

    assert asyncio.run(scenario()) is False


def test_restore_last_playback_ignores_movie_no_longer_in_library(tmp_path):
    state_path = tmp_path / "playback_state.json"
    state_path.write_text(json.dumps({"movie_id": 99999, "position_seconds": 5, "paused": False}))

    async def scenario():
        mpv = FakeMpv()
        controller = PlaybackController(FakeLibrary(), mpv, state_path=state_path)
        resumed = await controller.restore_last_playback()
        return controller, mpv, resumed

    controller, mpv, resumed = asyncio.run(scenario())
    assert resumed is False
    assert controller.is_idle
    assert mpv.loaded_path is None
    assert not state_path.exists()


def test_restore_last_playback_ignores_corrupt_state_file(tmp_path):
    state_path = tmp_path / "playback_state.json"
    state_path.write_text("not valid json")

    async def scenario():
        controller = PlaybackController(FakeLibrary(), FakeMpv(), state_path=state_path)
        return await controller.restore_last_playback()

    resumed = asyncio.run(scenario())
    assert resumed is False
    assert not state_path.exists()


def test_startup_continues_the_saved_movie_at_its_position(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"movie_id": 0, "position_seconds": 50, "paused": True}))

    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, state_path)
        await controller.resume_on_startup()
        assert player.loaded_path == FakeLibrary().path_for(0)
        assert player.position == 50
        assert player.paused

    asyncio.run(scenario())


def test_startup_shows_idle_screen_when_nothing_was_playing(tmp_path):
    state_path = tmp_path / "state.json"

    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, state_path)
        await controller.resume_on_startup()
        assert controller.is_idle
        assert player.loaded_path is None
        assert player.shown_image_path is not None

    asyncio.run(scenario())


def test_startup_input_prevents_autoplay():
    async def scenario():
        for opcode in (Opcode.STOP, Opcode.PAUSE, Opcode.SELECT_MOVIE):
            player = FakeMpv()
            controller = PlaybackController(FakeLibrary(), player)
            await controller.handle_command(Command(opcode=opcode, argument=0))
            previous_path = player.loaded_path
            await controller.resume_on_startup()
            assert player.loaded_path == previous_path
            if opcode == Opcode.PAUSE:
                assert player.paused

    asyncio.run(scenario())


def test_finished_movie_stops_instead_of_playing_another():
    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        player.finished = True
        player.idle = True
        state = await controller.refresh_status()
        assert state.status == PlaybackStatus.STOPPED
        assert player.shown_image_path is not None

    asyncio.run(scenario())


def test_empty_library_stays_idle():
    class EmptyLibrary(FakeLibrary):
        @property
        def movies(self):
            return []

    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(EmptyLibrary(), player)
        await controller.resume_on_startup()
        assert controller.is_idle
        assert player.loaded_path is None

    asyncio.run(scenario())


def test_background_playback_stops_finished_movie_without_transport_requests():
    from player_app.main import _run_playback

    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        player.finished = True
        player.idle = True
        stop = asyncio.Event()

        async def finish_polling(*args):
            stop.set()

        with patch("player_app.main.sleep_unless_stopped", side_effect=finish_polling):
            await _run_playback(controller, stop)
        assert controller.is_idle
        assert player.shown_image_path is not None

    asyncio.run(scenario())


def test_mpv_marks_only_natural_end_as_finished():
    from player_app.views.player import MpvController

    async def scenario(reason):
        player = MpvController()
        player._reader = asyncio.StreamReader()
        player._reader.feed_data(json.dumps({"event": "end-file", "reason": reason}).encode() + b"\n")
        player._reader.feed_eof()
        await player._listen()
        return player.finished

    assert asyncio.run(scenario("eof"))
    assert not asyncio.run(scenario("stop"))
    assert not asyncio.run(scenario("error"))


def test_stop_remembers_position_across_restart(tmp_path):
    async def scenario():
        state_path = tmp_path / "state.json"
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, state_path)
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        player.position = 37
        await controller.handle_command(Command(Opcode.STOP))
        reloaded = PlaybackController(FakeLibrary(), player, state_path)
        await reloaded.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        assert player.position == 37
    asyncio.run(scenario())


def test_finish_clears_bookmark_and_failed_movie_stops(tmp_path):
    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, tmp_path / "state.json")
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        player.position = 30
        await controller.refresh_status()
        player.finished = True
        await controller.refresh_status()
        assert controller._positions["0"] == 0
        assert controller.is_idle
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 1))
        player.failed = True
        state = await controller.refresh_status()
        assert state.status == PlaybackStatus.STOPPED
        assert controller.is_idle
        assert player.shown_image_path is not None
    asyncio.run(scenario())


def test_corrupt_positions_are_ignored(tmp_path):
    (tmp_path / "movie_positions.json").write_text('{"0": -9, "1": "bad"}')
    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, tmp_path / "state.json")
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        assert player.position == 0
    asyncio.run(scenario())


def test_installing_update_stops_the_movie_but_keeps_it_to_resume(tmp_path, monkeypatch):
    async def scenario():
        player = FakeMpv()
        state_path = tmp_path / "state.json"
        controller = PlaybackController(FakeLibrary(), player, state_path)
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        player.position = 123
        monkeypatch.setattr("player_app.controllers.playback_controller.read_status", lambda: "installing")
        assert (await controller.refresh_status()).status == PlaybackStatus.STOPPED
        assert controller.is_idle
        assert json.loads(state_path.read_text())["position_seconds"] == 123
        restarted = PlaybackController(FakeLibrary(), FakeMpv(), state_path)
        assert await restarted.restore_last_playback()
    asyncio.run(scenario())


def test_loading_does_not_erase_saved_position(tmp_path):
    async def scenario():
        player = FakeMpv()
        controller = PlaybackController(FakeLibrary(), player, tmp_path / "state.json")
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        player.position = 37
        await controller.handle_command(Command(Opcode.STOP))
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        player.loading = True
        player.position = 0
        state = await controller.refresh_status()
        assert state.position_seconds == 37
        assert controller._positions["0"] == 37
    asyncio.run(scenario())


def test_periodic_saves_every_five_seconds_with_live_status(tmp_path):
    async def scenario():
        player = FakeMpv()
        state_path = tmp_path / "state.json"
        controller = PlaybackController(FakeLibrary(), player, state_path)
        await controller.handle_command(Command(Opcode.SELECT_MOVIE, 0))
        with patch("player_app.controllers.playback_controller.time.monotonic", return_value=100) as clock:
            player.position = 10
            await controller.refresh_status()
            for seconds in (1, 2, 3, 4):
                clock.return_value = 100 + seconds
                player.position = 10 + seconds
                assert (await controller.refresh_status()).position_seconds == 10 + seconds
                assert json.loads(state_path.read_text())["position_seconds"] == 10
                assert json.loads((tmp_path / "movie_positions.json").read_text())["0"] == 10
            clock.return_value = 105
            player.position = 15
            await controller.refresh_status()
            assert json.loads(state_path.read_text())["position_seconds"] == 15
            clock.return_value = 106
            player.position = 16
            await controller.handle_command(Command(Opcode.STOP))
            assert json.loads((tmp_path / "movie_positions.json").read_text())["0"] == 16
    asyncio.run(scenario())


def test_save_position_now_writes_the_exact_position_for_every_movie(tmp_path):
    state_path = tmp_path / "playback_state.json"
    player = FakeMpv()

    async def scenario():
        controller = PlaybackController(FakeLibrary(), player, state_path=state_path)
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=0))
        player.position = 30
        await controller.handle_command(Command(opcode=Opcode.SELECT_MOVIE, argument=1))
        player.position = 55
        await controller.save_position_now()
        return controller

    controller = asyncio.run(scenario())
    positions = json.loads((tmp_path / "movie_positions.json").read_text())
    assert positions == {"0": 30, "1": 55}
    assert json.loads(state_path.read_text())["position_seconds"] == 55
