import asyncio

from aioresponses import aioresponses

from player_app.models.library import MovieLibrary
from player_app.services.home_sync_service import HomeServerSync

BASE_URL = "http://home.example.com:8080"


def _library(tmp_path):
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    return MovieLibrary(movies_dir, thumbnail_dir=tmp_path / "thumbnails")


def test_check_in_downloads_ready_movie_and_saves_metadata(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        # repeat=True: called once for the Items-listing fallback (below)
        # and once more for the download leg's own separate auth - see
        # check_in()'s comment on why downloading always re-authenticates.
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={
                "Items": [
                    {
                        "Id": "1",
                        "Name": "Alpha",
                        "Overview": "A movie.",
                        "ProductionYear": 1999,
                        "RunTimeTicks": 1234000000,
                        "MagicBoxieOriginalFilename": "alpha.mkv",
                        "MagicBoxieStatus": "ready",
                    },
                    {
                        "Id": "2",
                        "Name": "Still Transcoding",
                        "MagicBoxieStatus": "needs_transcode",
                    },
                ],
                "TotalRecordCount": 2,
                "StartIndex": 0,
            },
        )
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"fake-video-bytes")

        asyncio.run(sync.check_in())

    titles = [m.title for m in library.movies]
    assert titles == ["Alpha"]
    assert (tmp_path / "movies" / "Alpha.mkv").read_bytes() == b"fake-video-bytes"

    movie = library.movies[0]
    assert library.metadata_for(movie.id) == {
        "title": "Alpha",
        "description": "A movie.",
        "year": 1999,
        "duration_seconds": 123,
        "player_copy_version": 0,
    }


def test_check_in_skips_movie_already_present_locally(tmp_path):
    library = _library(tmp_path)
    (tmp_path / "movies" / "Alpha.mp4").write_bytes(b"already-here")
    library.scan()
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={
                "Items": [
                    {
                        "Id": "1",
                        "Name": "Alpha",
                        "MagicBoxieStatus": "ready",
                        "MagicBoxieOriginalFilename": "alpha.mp4",
                    }
                ],
                "TotalRecordCount": 1,
                "StartIndex": 0,
            },
        )
        # No /stream mock registered - if the code tries to download despite
        # the movie already being local, aioresponses raises for the
        # unmatched request and the test fails.
        asyncio.run(sync.check_in())

    assert (tmp_path / "movies" / "Alpha.mp4").read_bytes() == b"already-here"


def test_check_in_registers_and_downloads_content_when_available(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, "https://magicboxie.com", "secret")

    with aioresponses() as mocked:
        mocked.post(
            "https://magicboxie.com/devices/register",
            payload={
                "items": [
                    {
                        "Id": "1",
                        "Name": "Beta",
                        "Overview": "A registered movie.",
                        "ProductionYear": 2001,
                        "RunTimeTicks": 987654321,
                        "MagicBoxieOriginalFilename": "beta.mkv",
                        "MagicBoxieStatus": "ready",
                    }
                ]
            },
        )
        # /devices/register lists movies without authenticating, but the
        # video bytes themselves are always served from an authenticated
        # route - see check_in()'s own comment on why.
        mocked.post("https://magicboxie.com/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get("https://magicboxie.com/Videos/1/stream?static=true", body=b"registered-video-bytes")

        asyncio.run(sync.check_in())

    assert (tmp_path / "movies" / "Beta.mkv").read_bytes() == b"registered-video-bytes"
    movie = library.movies[0]
    assert library.metadata_for(movie.id) == {
        "title": "Beta",
        "description": "A registered movie.",
        "year": 2001,
        "duration_seconds": 98,
        "player_copy_version": 0,
    }


def test_check_in_does_not_download_unauthenticated_when_registration_listing_worked(tmp_path):
    """/devices/register lists movies without a password, but the actual
    video bytes are served from an authenticated route - a device with the
    wrong (or no) HOME_SERVER_PASSWORD must not attempt an unauthenticated
    download that would just 401, and definitely must not crash trying."""
    library = _library(tmp_path)
    sync = HomeServerSync(library, "https://magicboxie.com", "wrong-password")

    with aioresponses() as mocked:
        mocked.post(
            "https://magicboxie.com/devices/register",
            payload={
                "items": [
                    {"Id": "1", "Name": "Gamma", "MagicBoxieOriginalFilename": "gamma.mp4", "MagicBoxieStatus": "ready"}
                ]
            },
        )
        mocked.post("https://magicboxie.com/Users/AuthenticateByName", status=401, payload={"error": "invalid password"})
        # No /Videos/1/stream mock registered - if the code tries to
        # download without a token anyway, aioresponses raises for the
        # unmatched request and the test fails.
        asyncio.run(sync.check_in())

    assert library.movies == []


def test_check_in_does_nothing_when_login_fails(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "wrong-password")

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", status=401, payload={"error": "invalid password"})
        # No /Items mock registered - a login failure must stop check_in()
        # before it lists movies, or aioresponses raises for the unmatched call.
        asyncio.run(sync.check_in())

    assert library.movies == []


def test_check_in_handles_unreachable_movies_list(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true", status=500)
        asyncio.run(sync.check_in())

    assert library.movies == []


def test_check_in_downloads_one_at_a_time_reporting_progress(tmp_path):
    """The device only ever has one movie in flight at once - downloads are
    large files over a slow link, and on_progress (which
    main.py mirrors onto PlaybackController.currently_syncing_movie_title
    for /api/status to report) only ever tracks a single title, so this is
    both a behavioral guarantee and what makes that reporting meaningful."""
    library = _library(tmp_path)
    progress_calls = []
    sync = HomeServerSync(library, BASE_URL, "secret", on_progress=progress_calls.append)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={
                "Items": [
                    {"Id": "1", "Name": "Alpha", "MagicBoxieOriginalFilename": "alpha.mp4", "MagicBoxieStatus": "ready"},
                    {"Id": "2", "Name": "Beta", "MagicBoxieOriginalFilename": "beta.mp4", "MagicBoxieStatus": "ready"},
                ],
            },
        )
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"alpha-bytes")
        mocked.get(f"{BASE_URL}/Videos/2/stream?static=true", body=b"beta-bytes")

        asyncio.run(sync.check_in())

    # Alpha's whole in-flight window (start, then end) happens before Beta's
    # starts - never both "in flight" (non-None) at once.
    assert progress_calls == ["Alpha", None, "Beta", None]


def test_check_in_clears_progress_even_when_a_download_fails(tmp_path):
    library = _library(tmp_path)
    progress_calls = []
    sync = HomeServerSync(library, BASE_URL, "secret", on_progress=progress_calls.append)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={
                "Items": [
                    {"Id": "1", "Name": "Alpha", "MagicBoxieOriginalFilename": "alpha.mp4", "MagicBoxieStatus": "ready"},
                ],
            },
        )
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", status=500)

        asyncio.run(sync.check_in())

    assert progress_calls == ["Alpha", None]


def test_check_in_downloads_while_playing(tmp_path):
    """Downloads take priority over everything, playback included."""
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret", is_idle=lambda: False)
    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={"Items": [
                {"Id": "1", "Name": "Alpha", "MagicBoxieOriginalFilename": "alpha.mp4", "MagicBoxieStatus": "ready"},
                {"Id": "2", "Name": "Beta", "MagicBoxieOriginalFilename": "beta.mp4", "MagicBoxieStatus": "ready"},
            ]},
        )
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"alpha-bytes")
        mocked.get(f"{BASE_URL}/Videos/2/stream?static=true", body=b"beta-bytes")
        asyncio.run(sync.check_in())

    assert (tmp_path / "movies" / "Alpha.mp4").exists()
    assert (tmp_path / "movies" / "Beta.mp4").exists()


def test_check_in_defers_scan_and_metadata_when_playback_starts_mid_cycle(tmp_path):
    """Alpha finishes downloading right as playback starts (before this
    cycle reaches library.scan()) - the file must stay on disk and its
    metadata must not be lost, just deferred to a later, idle check-in."""
    library = _library(tmp_path)
    idle = [True]
    sync = HomeServerSync(library, BASE_URL, "secret", is_idle=lambda: idle[0])

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true",
            payload={
                "Items": [
                    {
                        "Id": "1",
                        "Name": "Alpha",
                        "Overview": "A movie.",
                        "ProductionYear": 1999,
                        "RunTimeTicks": 1234000000,
                        "MagicBoxieOriginalFilename": "alpha.mkv",
                        "MagicBoxieStatus": "ready",
                    },
                ],
            },
            repeat=True,
        )
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"alpha-bytes")

        # Flips right as Alpha's download finishes (on_progress(None)),
        # still inside this same check_in() call - i.e. after the download
        # but before the library.scan() step at the end. Only for this one
        # call - the second check_in() below re-fires on_progress for the
        # same (already-downloaded, no-op) movie, and must not re-trigger this.
        def stop_being_idle_once(title):
            if title is None:
                idle[0] = False

        sync._on_progress = stop_being_idle_once
        asyncio.run(sync.check_in())

        # Still on disk but not yet scanned into the library - deferred, not lost.
        assert (tmp_path / "movies" / "Alpha.mkv").exists()
        assert library.movies == []

        # Playback ends; the next check-in picks up the deferred scan/metadata.
        sync._on_progress = lambda _title: None
        idle[0] = True
        asyncio.run(sync.check_in())

    titles = [m.title for m in library.movies]
    assert titles == ["Alpha"]
    movie = library.movies[0]
    assert library.metadata_for(movie.id) == {
        "title": "Alpha",
        "description": "A movie.",
        "year": 1999,
        "duration_seconds": 123,
        "player_copy_version": 0,
    }


def _items(*names):
    return {
        "Items": [
            {"Id": str(i), "Name": name, "MagicBoxieStatus": "ready", "MagicBoxieOriginalFilename": f"{name}.mp4"}
            for i, name in enumerate(names, start=1)
        ],
        "TotalRecordCount": len(names),
        "StartIndex": 0,
    }


def test_check_in_is_busy_while_downloading_and_clears_afterwards(tmp_path):
    library = _library(tmp_path)
    states = []
    sync = HomeServerSync(library, BASE_URL, "secret", on_busy=states.append)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok"}, repeat=True)
        mocked.get(f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true", payload=_items("Alpha"))
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"bytes")
        asyncio.run(sync.check_in())

    assert states == [True, False]


def test_check_in_is_not_busy_when_there_is_nothing_to_download(tmp_path):
    library = _library(tmp_path)
    (tmp_path / "movies" / "Alpha.mp4").write_bytes(b"here")
    library.scan()
    states = []
    sync = HomeServerSync(library, BASE_URL, "secret", on_busy=states.append)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok"}, repeat=True)
        mocked.get(f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true", payload=_items("Alpha"))
        asyncio.run(sync.check_in())

    assert states == [False]


def test_busy_is_cleared_even_if_the_check_in_raises(tmp_path):
    library = _library(tmp_path)
    states = []
    sync = HomeServerSync(library, BASE_URL, "secret", on_busy=states.append)

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    sync._download_movie = boom
    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok"}, repeat=True)
        mocked.get(f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true", payload=_items("Alpha"))
        try:
            asyncio.run(sync.check_in())
        except RuntimeError:
            pass

    assert states[-1] is False


def test_check_in_tracks_the_download_queue_and_home_server_preparing(tmp_path):
    library = _library(tmp_path)
    queued_at_start = []
    sync = HomeServerSync(
        library, BASE_URL, "secret",
        on_progress=lambda title: title and queued_at_start.append((title, list(sync.activity.queued))),
    )
    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload={
            "Items": [
                {"Id": "1", "Name": "Alpha", "MagicBoxieOriginalFilename": "alpha.mp4"},
                {"Id": "2", "Name": "Beta", "MagicBoxieOriginalFilename": "beta.mp4"},
            ],
            "MagicBoxiePreparing": [{"Name": "Gamma", "Status": "transcoding", "ProgressPercent": 42.5}],
        })
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"alpha-bytes")
        mocked.get(f"{BASE_URL}/Videos/2/stream?static=true", body=b"beta-bytes")
        asyncio.run(sync.check_in())

    assert queued_at_start == [("Alpha", ["Beta"]), ("Beta", [])]
    assert sync.activity.queued == []
    assert sync.activity.reachable is True
    assert sync.activity.preparing == [{"title": "Gamma", "status": "transcoding", "progress_percent": 42.5}]


def test_check_in_while_playing_still_lists_what_is_waiting(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret", is_idle=lambda: False)
    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload={"items": [{"id": "1", "name": "Alpha"}]})
        asyncio.run(sync.check_in())
    assert sync.activity.queued == ["Alpha"]


def _registered(player_status):
    movie = {"Id": "1", "Name": "Gamma", "MagicBoxieOriginalFilename": "gamma.mkv", "MagicBoxieStatus": "ready"}
    if player_status is not None:
        movie["MagicBoxiePlayerStatus"] = player_status
    return {"items": [movie]}


def test_check_in_downloads_the_home_servers_480p_copy(tmp_path):
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    library = MovieLibrary(movies_dir, thumbnail_dir=tmp_path / "thumbnails", transcode_dir=tmp_path / "transcoded")
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered("ready"))
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(f"{BASE_URL}/Videos/1/player", body=b"480p-bytes")

        asyncio.run(sync.check_in())

    # Always an .mp4, whatever the original was.
    assert (movies_dir / "Gamma.mp4").read_bytes() == b"480p-bytes"
    assert library.playable_path_for(library.movies[0].id) == movies_dir / "Gamma.mp4"


def test_check_in_waits_for_the_home_servers_480p_copy(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        # Neither a login nor a download: aioresponses raises for either.
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered("pending"))

        asyncio.run(sync.check_in())

    assert library.movies == []
    assert sync.activity.queued == []


def test_check_in_downloads_the_full_file_when_the_480p_copy_failed(tmp_path):
    library = _library(tmp_path)
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered("error"))
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"full-size-bytes")

        asyncio.run(sync.check_in())

    assert (tmp_path / "movies" / "Gamma.mkv").read_bytes() == b"full-size-bytes"


def _registered_copy(version):
    payload = _registered("ready")
    payload["items"][0]["MagicBoxiePlayerVersion"] = version
    return payload


def test_check_in_swaps_a_local_original_for_the_servers_newer_copy(tmp_path):
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "Gamma.mkv").write_bytes(b"full-size-bytes")
    library = MovieLibrary(movies_dir, thumbnail_dir=tmp_path / "thumbnails", transcode_dir=tmp_path / "transcoded")
    library.scan()
    movie_id = library.movies[0].id
    # Left over from when the player transcoded movies itself.
    (tmp_path / "transcoded").mkdir()
    library.transcode_path_for(movie_id).write_bytes(b"old-transcode")
    sync = HomeServerSync(library, BASE_URL, "secret", is_idle=lambda: False)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered_copy(2))
        mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "tok123"})
        mocked.get(f"{BASE_URL}/Videos/1/player", body=b"480p-bytes")
        asyncio.run(sync.check_in())

    assert not (movies_dir / "Gamma.mkv").exists()
    # Same movie (id, thumbnail, metadata), now playing the copy - even
    # before the deferred rescan.
    assert library.playable_path_for(movie_id).read_bytes() == b"480p-bytes"
    assert library.metadata_for(movie_id)["player_copy_version"] == 2

    with aioresponses() as mocked:
        # Already current: no login, no download.
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered_copy(2))
        asyncio.run(sync.check_in())
    assert sync.activity.queued == []

    sync._is_idle = lambda: True
    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered_copy(2))
        asyncio.run(sync.check_in())
    assert [m.id for m in library.movies] == [movie_id]
    assert library.metadata_for(movie_id)["player_copy_version"] == 2


def test_check_in_keeps_a_local_movie_when_the_server_has_no_newer_copy(tmp_path):
    library = _library(tmp_path)
    (library.root / "Gamma.mkv").write_bytes(b"full-size-bytes")
    library.scan()
    sync = HomeServerSync(library, BASE_URL, "secret")

    with aioresponses() as mocked:
        # An unversioned copy (older server): no login, no download.
        mocked.post(f"{BASE_URL}/devices/register", payload=_registered("ready"))
        asyncio.run(sync.check_in())

    assert (library.root / "Gamma.mkv").read_bytes() == b"full-size-bytes"


def test_download_is_throttled_only_while_playing(tmp_path, monkeypatch):
    from player_app.services import home_sync_service
    monkeypatch.setattr(home_sync_service, "_DOWNLOAD_CHUNK_BYTES", 10)
    monkeypatch.setattr(home_sync_service, "_PLAYING_DOWNLOAD_BYTES_PER_SECOND", 100)  # 0.1s per 10-byte chunk
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(home_sync_service.asyncio, "sleep", fake_sleep)

    def download(idle):
        root = tmp_path / ("idle" if idle else "playing")
        root.mkdir()
        library = _library(root)
        sync = HomeServerSync(library, BASE_URL, "secret", is_idle=lambda: idle)
        with aioresponses() as mocked:
            mocked.post(f"{BASE_URL}/Users/AuthenticateByName", payload={"AccessToken": "t"}, repeat=True)
            mocked.get(f"{BASE_URL}/Users/1/Items?IncludeItemTypes=Movie&Recursive=true", payload={"Items": [
                {"Id": "1", "Name": "Alpha", "MagicBoxieOriginalFilename": "alpha.mp4", "MagicBoxieStatus": "ready"}]})
            mocked.get(f"{BASE_URL}/Videos/1/stream?static=true", body=b"x" * 30)
            asyncio.run(sync.check_in())

    download(idle=True)
    assert sleeps == []
    download(idle=False)
    assert len(sleeps) == 3 and all(0 < s <= 0.1 for s in sleeps)
