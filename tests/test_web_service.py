import asyncio
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from fakes import FakeLibrary, FakeMpv

from player_app.controllers.playback_controller import PlaybackController
from player_app.models import protocol
from player_app.models.library import MovieLibrary
from player_app.util import ThrottleStatus
from web.web_service import create_app


async def _make_client(library=None):
    controller = PlaybackController(library or FakeLibrary(), FakeMpv())
    app = create_app(controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, controller


def test_get_movies():
    async def scenario():
        # B is the media server's 480p copy; A still needs transcoding.
        client, _ = await _make_client(FakeLibrary(metadata={1: {"player_copy_version": 2}}))
        try:
            resp = await client.get("/api/movies")
            assert resp.status == 200
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data == [
        {"id": 0, "title": "A", "duration_seconds": 100, "description": None, "year": None, "needs_transcoding": True, "position_seconds": 0},
        {"id": 1, "title": "B", "duration_seconds": 200, "description": None, "year": None, "needs_transcoding": False, "position_seconds": 0,
         "player_copy_version": 2},
    ]


def test_get_movies_reports_where_a_previously_played_movie_left_off():
    async def scenario():
        client, controller = await _make_client()
        try:
            controller._set_position(0, 42)
            # Played to the end: nothing left to resume, so no progress.
            controller._set_position(1, 200)
            resp = await client.get("/api/movies")
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    by_id = {movie["id"]: movie for movie in data}
    assert by_id[0]["position_seconds"] == 42
    assert by_id[1]["position_seconds"] == 0


def test_get_version():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.get("/api/version")
            assert resp.status == 200
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["api_version"] == protocol.API_VERSION
    # Best-effort LAN IP - not asserting an exact value (depends on the
    # test host's own network config), just that it's a plausible one.
    assert data["ip_address"].count(".") == 3


def test_post_command_select_and_play_reflected_in_status():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/command", json={"opcode": "select_movie", "argument": 1})
            assert resp.status == 200
            resp = await client.get("/api/status")
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["status"] == "playing"
    assert data["movie_id"] == 1


def test_status_reports_currently_syncing_movie_title():
    async def scenario():
        client, controller = await _make_client()
        try:
            controller.currently_syncing_movie_title = "Some Movie"
            resp = await client.get("/api/status")
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["syncing_movie_title"] == "Some Movie"


def test_status_reports_cpu_temperature():
    async def scenario():
        client, _ = await _make_client()
        try:
            with patch("web.web_service.cpu_temperature_celsius", return_value=48.3):
                resp = await client.get("/api/status")
                return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["cpu_temperature_celsius"] == 48.3


def test_status_reports_null_temperature_when_sensor_unavailable():
    """The Docker dev container this test suite runs in has no real
    thermal zone - cpu_temperature_celsius() already handles that
    gracefully (see its own test), and this just confirms the endpoint
    doesn't choke on a None."""
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.get("/api/status")
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["cpu_temperature_celsius"] is None


def test_status_reports_throttle_flags():
    async def scenario():
        client, _ = await _make_client()
        try:
            with patch(
                "web.web_service.get_throttle_status",
                new=AsyncMock(return_value=ThrottleStatus(under_voltage=True, throttled=False)),
            ):
                resp = await client.get("/api/status")
                return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["under_voltage"] is True
    assert data["throttled"] is False


def test_status_reports_null_throttle_flags_when_vcgencmd_unavailable():
    """The Docker dev container this test suite runs in has no vcgencmd -
    get_throttle_status() already handles that gracefully (see its own
    test), and this just confirms the endpoint doesn't choke on a None."""
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.get("/api/status")
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["under_voltage"] is None
    assert data["throttled"] is None


def test_post_command_unknown_opcode_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/command", json={"opcode": "not_a_real_command"})
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_get_thumbnail_returns_file_bytes(tmp_path):
    thumbnail_path = tmp_path / "0.jpg"
    thumbnail_path.write_bytes(b"fake-jpeg-bytes")

    async def scenario():
        client, _ = await _make_client(FakeLibrary(thumbnail_paths={0: thumbnail_path}))
        try:
            resp = await client.get("/api/movies/0/thumbnail")
            assert resp.status == 200
            return await resp.read()
        finally:
            await client.close()

    assert asyncio.run(scenario()) == b"fake-jpeg-bytes"


def test_get_thumbnail_missing_returns_404():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.get("/api/movies/0/thumbnail")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 404


def test_get_thumbnail_invalid_id_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.get("/api/movies/not-a-number/thumbnail")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_thumbnail_upload_then_get_returns_uploaded_bytes(tmp_path):
    async def scenario():
        client, _ = await _make_client(FakeLibrary(thumbnail_dir=tmp_path))
        try:
            resp = await client.post("/api/movies/0/thumbnail", data=b"official-poster-bytes")
            assert resp.status == 200
            resp = await client.get("/api/movies/0/thumbnail")
            assert resp.status == 200
            return await resp.read()
        finally:
            await client.close()

    assert asyncio.run(scenario()) == b"official-poster-bytes"


def test_post_thumbnail_unknown_movie_returns_404():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/99/thumbnail", data=b"bytes")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 404


def test_post_thumbnail_invalid_id_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/not-a-number/thumbnail", data=b"bytes")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_thumbnail_empty_body_returns_400(tmp_path):
    async def scenario():
        client, _ = await _make_client(FakeLibrary(thumbnail_dir=tmp_path))
        try:
            resp = await client.post("/api/movies/0/thumbnail", data=b"")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_metadata_overrides_fields_in_movie_list():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post(
                "/api/movies/0/metadata",
                json={"title": "Alpha", "description": "A movie.", "year": 1999},
            )
            assert resp.status == 200
            body = await resp.json()

            resp = await client.get("/api/movies")
            return body, await resp.json()
        finally:
            await client.close()

    post_body, movies = asyncio.run(scenario())
    expected = {
        "id": 0,
        "title": "Alpha",
        "duration_seconds": 100,
        "description": "A movie.",
        "year": 1999,
        "needs_transcoding": True,
        "position_seconds": 0,
    }
    assert post_body == expected
    assert movies[0] == expected
    assert movies[1] == {
        "id": 1, "title": "B", "duration_seconds": 200, "description": None, "year": None, "needs_transcoding": True,
        "position_seconds": 0,
    }


def test_post_metadata_unknown_movie_returns_404():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/99/metadata", json={"title": "X"})
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 404


def test_post_metadata_invalid_id_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/not-a-number/metadata", json={"title": "X"})
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_metadata_non_integer_year_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/0/metadata", json={"year": "not-a-year"})
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_metadata_empty_body_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.post("/api/movies/0/metadata", json={})
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def _real_library(tmp_path):
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    library = MovieLibrary(movies_dir, thumbnail_dir=tmp_path / "thumbnails")
    library.scan()
    return library


def test_post_movie_uploads_and_appears_in_library(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post(
                "/api/movies",
                data=b"fake-video-bytes",
                headers={"X-Filename": "New Movie.mp4"},
            )
            assert resp.status == 201
            body = await resp.json()
            assert body["title"] == "New Movie"

            resp = await client.get("/api/movies")
            return await resp.json()
        finally:
            await client.close()

    movies = asyncio.run(scenario())
    assert [m["title"] for m in movies] == ["New Movie"]
    assert (tmp_path / "movies" / "New Movie.mp4").read_bytes() == b"fake-video-bytes"


def test_post_rescan_picks_up_files_added_directly_to_the_filesystem(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            (tmp_path / "movies" / "Added Directly.mp4").write_bytes(b"fake-video-bytes")

            resp = await client.post("/api/rescan")
            assert resp.status == 200
            return await resp.json()
        finally:
            await client.close()

    movies = asyncio.run(scenario())
    assert [m["title"] for m in movies] == ["Added Directly"]


def test_post_movie_missing_filename_header_returns_400(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post("/api/movies", data=b"bytes")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_movie_unsupported_extension_returns_400(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post(
                "/api/movies", data=b"bytes", headers={"X-Filename": "not-a-video.txt"}
            )
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_post_movie_duplicate_filename_returns_409(tmp_path):
    library = _real_library(tmp_path)
    (tmp_path / "movies" / "Existing.mp4").write_bytes(b"already-here")

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post(
                "/api/movies", data=b"new-bytes", headers={"X-Filename": "Existing.mp4"}
            )
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 409


def test_post_movie_empty_body_returns_400(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post(
                "/api/movies", data=b"", headers={"X-Filename": "Empty.mp4"}
            )
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400
    assert not (tmp_path / "movies" / "Empty.mp4").exists()


def test_delete_movie_removes_it_from_disk_and_the_list(tmp_path):
    library = _real_library(tmp_path)
    (tmp_path / "movies" / "Doomed.mp4").write_bytes(b"fake-video-bytes")
    library.scan()
    movie_id = next(m.id for m in library.movies if m.title == "Doomed")

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.delete(f"/api/movies/{movie_id}")
            assert resp.status == 200
            resp = await client.get("/api/movies")
            return await resp.json()
        finally:
            await client.close()

    movies = asyncio.run(scenario())
    assert not any(m["title"] == "Doomed" for m in movies)
    assert not (tmp_path / "movies" / "Doomed.mp4").exists()


def test_delete_movie_unknown_id_returns_404():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.delete("/api/movies/99999")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 404


def test_delete_movie_invalid_id_returns_400():
    async def scenario():
        client, _ = await _make_client()
        try:
            resp = await client.delete("/api/movies/not-a-number")
            return resp.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == 400


def test_delete_movie_stops_playback_first_if_currently_selected():
    async def scenario():
        client, controller = await _make_client()
        try:
            await client.post("/api/command", json={"opcode": "select_movie", "argument": 0})
            resp = await client.delete("/api/movies/0")
            assert resp.status == 200
            return controller
        finally:
            await client.close()

    controller = asyncio.run(scenario())
    # Mirrors test_stop_shows_idle_screen's own assertion: confirms
    # stop_and_show_idle_screen ran (rather than the file being deleted out
    # from under active playback) without depending on FakeMpv.idle, which
    # goes back to False once the idle-screen image itself gets "loaded".
    assert controller.player.shown_image_path is not None


def test_portal_is_an_offline_html_page():
    async def scenario():
        client, _ = await _make_client()
        try:
            response = await client.get("/")
            assert response.status == 200
            assert response.content_type == "text/html"
            html = await response.text()
            assert "MagicBoxie Player" in html
            assert "https://" not in html
            assert response.headers["Cache-Control"] == "no-store"
            script = await client.get("/static/app.js")
            assert script.status == 200
            assert "fetch(path, options)" in await script.text()
            assert script.headers["Cache-Control"] == "no-cache"
            stylesheet = await client.get("/static/style.css")
            assert stylesheet.status == 200
            assert (await client.get("/static/missing.js")).status == 404
        finally:
            await client.close()
    asyncio.run(scenario())


def test_captive_portal_probes_redirect_to_fixed_device_address():
    async def scenario():
        client, _ = await _make_client()
        try:
            for path in ("/generate_204", "/gen_204", "/hotspot-detect.html",
                         "/library/test/success.html", "/connecttest.txt",
                         "/ncsi.txt", "/redirect", "/unknown?next=https://example.com"):
                response = await client.get(path, headers={"Host": "untrusted.example"}, allow_redirects=False)
                assert response.status == 302
                assert response.headers["Location"] == "http://10.42.0.1/welcome"
                assert response.headers["Cache-Control"] == "no-store"
            response = await client.head("/generate_204", allow_redirects=False)
            assert response.status == 302
            response = await client.get("/api/unknown", allow_redirects=False)
            assert response.status == 404
            response = await client.get("/api/movies", headers={"Host": "10.42.0.1"})
            assert response.status == 200
        finally:
            await client.close()
    asyncio.run(scenario())


def test_status_reports_internet_reachable():
    async def scenario():
        client, _ = await _make_client()
        try:
            with patch(
                "web.web_service.internet_reachable",
                new=AsyncMock(return_value=True),
            ):
                resp = await client.get("/api/status")
                return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["internet_reachable"] is True


def test_status_reports_internet_unreachable():
    async def scenario():
        client, _ = await _make_client()
        try:
            with patch(
                "web.web_service.internet_reachable",
                new=AsyncMock(return_value=False),
            ):
                resp = await client.get("/api/status")
                return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["internet_reachable"] is False


def test_info_reports_device_details():
    async def scenario():
        client, _ = await _make_client()
        try:
            with patch("web.web_service.get_throttle_status", new=AsyncMock(return_value=None)), \
                    patch("web.web_service.internet_reachable", new=AsyncMock(return_value=True)), \
                    patch("web.web_service.system_info.snapshot",
                          return_value={"hostname": "magicboxie-player", "mdns_name": "magicboxie-player.local"}):
                response = await client.get("/api/info")
                assert response.status == 200
                return await response.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["hostname"] == "magicboxie-player"
    assert data["internet_reachable"] is True
    assert data["movie_count"] == 2
    assert data["keyboards"] == []
    assert data["playback_status"] == "stopped"


def test_logs_passes_source_and_clamps_lines():
    async def scenario(query):
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.logs", return_value={"journal": ""}) as logs:
                response = await client.get("/api/logs" + query)
                return response.status, logs.call_args
        finally:
            await client.close()

    status, call = asyncio.run(scenario("?source=player&lines=999999"))
    assert status == 200 and call.args == ("player", 2000)
    status, call = asyncio.run(scenario(""))
    assert status == 200 and call.args == ("wifi", 300)
    assert asyncio.run(scenario("?source=secrets"))[0] == 400
    assert asyncio.run(scenario("?lines=lots"))[0] == 400


def test_wifi_logs_summarize_wifi_and_never_show_passwords(tmp_path):
    from web import system_info

    networks = tmp_path / "wifi.json"
    networks.write_text('{"version": 1, "networks": [{"ssid": "Mitera", "password": "secret"}]}')
    nmcli = {
        "connection": "Mitera home:802-11-wireless:yes\nWired:802-3-ethernet:yes\nmagicboxie-hotspot:802-11-wireless:no",
        "device": "eth0:unavailable:\nwlan0:connected:magicboxie-hotspot",
        "wifi": "40:Mitera\n72:Mitera\n55:Neighbor\n30:",
    }

    def run(*command, timeout=3):
        if command[0] == "nmcli":
            return nmcli["wifi" if "wifi" in command else command[-1] if command[-1] == "device" else "connection"]
        return " ".join(command)

    with patch("player_app.wifi_networks.load_networks.__defaults__", (networks,)), \
            patch.object(system_info, "_run", side_effect=run):
        data = system_info.logs("wifi", 50)
    assert "-u magicboxie-wifi-startup" in data["journal"] and "-n 50" in data["journal"]
    assert data["saved_networks"] == ["Mitera"]
    assert "secret" not in str(data)
    assert data["wifi_profiles"] == [
        {"name": "Mitera home", "autoconnect": True}, {"name": "magicboxie-hotspot", "autoconnect": False},
    ]
    assert data["adapter"] == "connected · magicboxie-hotspot"
    assert data["in_range"] == [{"ssid": "Mitera", "signal": 72}, {"ssid": "Neighbor", "signal": 55}]


def test_player_logs_have_no_wifi_summary():
    from web import system_info

    with patch.object(system_info, "_run", return_value="line"):
        data = system_info.logs("player")
    assert data == {"source": "player", "journal": "line"}


def test_reboot_reports_success_and_failure():
    async def scenario(error):
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.reboot", new=AsyncMock(return_value=error)):
                response = await client.post("/api/reboot")
                return response.status, await response.json()
        finally:
            await client.close()

    assert asyncio.run(scenario(None)) == (200, {"ok": True})
    status, body = asyncio.run(scenario("not permitted"))
    assert status == 500 and body["error"] == "not permitted"


def test_post_movie_accepts_a_percent_encoded_filename(tmp_path):
    library = _real_library(tmp_path)

    async def scenario():
        client, _ = await _make_client(library)
        try:
            resp = await client.post(
                "/api/movies",
                data=b"fake-video-bytes",
                headers={"X-Filename": "Caf%C3%A9%20Film.mp4", "X-Filename-Encoding": "uri"},
            )
            assert resp.status == 201
            traversal = await client.post(
                "/api/movies",
                data=b"x",
                headers={"X-Filename": "..%2Fescape.mp4", "X-Filename-Encoding": "uri"},
            )
            assert traversal.status == 400
        finally:
            await client.close()

    asyncio.run(scenario())
    assert (tmp_path / "movies" / "Café Film.mp4").read_bytes() == b"fake-video-bytes"
    assert not (tmp_path / "escape.mp4").exists()


def test_done_releases_only_that_client_from_the_probe_redirect():
    async def scenario():
        client, _ = await _make_client()
        try:
            probes = {"/generate_204": 204, "/hotspot-detect.html": 200, "/connecttest.txt": 200}
            for path in probes:
                assert (await client.get(path, allow_redirects=False)).status == 302
            done = await client.post("/api/portal/done")
            assert done.status == 200
            for path, status in probes.items():
                response = await client.get(path, allow_redirects=False)
                assert response.status == status
            assert "Success" in await (await client.get("/hotspot-detect.html")).text()
            # Other unknown pages still lead to the welcome page.
            other = await client.get("/something-else", allow_redirects=False)
            assert other.status == 302
        finally:
            await client.close()
    asyncio.run(scenario())


def test_welcome_page_only_points_to_the_browser():
    async def scenario():
        client, _ = await _make_client()
        try:
            response = await client.get("/welcome")
            assert response.status == 200
            html = await response.text()
            assert "http://10.42.0.1/" in html
            assert "/api/" not in html
            assert (await client.get("/static/welcome.js")).status == 200
        finally:
            await client.close()
    asyncio.run(scenario())


def test_shutdown_reports_success_and_failure():
    async def scenario(error):
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.shutdown", new=AsyncMock(return_value=error)):
                response = await client.post("/api/shutdown")
                return response.status, await response.json()
        finally:
            await client.close()

    assert asyncio.run(scenario(None)) == (200, {"ok": True})
    status, body = asyncio.run(scenario("not permitted"))
    assert status == 500 and body["error"] == "not permitted"


def test_status_tells_the_page_how_often_to_poll_and_any_thermal_note():
    async def scenario(thermal_note):
        client, controller = await _make_client()
        try:
            controller.thermal_note = thermal_note
            with patch("web.web_service.get_throttle_status", new=AsyncMock(return_value=None)), \
                    patch("web.web_service.internet_reachable", new=AsyncMock(return_value=True)):
                response = await client.get("/api/status")
                return await response.json()
        finally:
            await client.close()

    data = asyncio.run(scenario(None))
    assert data["poll_seconds"] == 3 and data["thermal_note"] is None
    assert asyncio.run(scenario("Too hot"))["thermal_note"] == "Too hot"


def test_wifi_search_reports_success_and_failure():
    async def scenario(error):
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.search_wifi", new=AsyncMock(return_value=error)):
                response = await client.post("/api/wifi/search")
                return response.status, await response.json()
        finally:
            await client.close()

    assert asyncio.run(scenario(None)) == (200, {"ok": True})
    assert asyncio.run(scenario("nope"))[0] == 500


def test_update_reports_success_and_failure():
    async def scenario(error):
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.start_update", new=AsyncMock(return_value=error)):
                response = await client.post("/api/update")
                return response.status, await response.json()
        finally:
            await client.close()

    assert asyncio.run(scenario(None)) == (200, {"ok": True})
    assert asyncio.run(scenario("nope"))[0] == 500


def test_wifi_networks_can_be_saved_and_listed_without_passwords(tmp_path):
    path = tmp_path / "wifi.json"

    async def scenario():
        client, _ = await _make_client()
        try:
            with patch("web.web_service.load_networks", side_effect=lambda: load_networks(path)), \
                    patch("web.web_service.save_network", side_effect=lambda s, p: save_network(s, p, path)):
                bad = await client.post("/api/wifi/networks", json={"ssid": "", "password": "x"})
                missing = await client.post("/api/wifi/networks", json={"password": "x"})
                ok = await client.post("/api/wifi/networks", json={"ssid": "My iPhone", "password": "secret"})
                listed = await (await client.get("/api/wifi/networks")).json()
                return bad.status, missing.status, ok.status, listed
        finally:
            await client.close()

    from player_app.wifi_networks import load_networks, save_network
    assert asyncio.run(scenario()) == (400, 400, 200, {"networks": ["My iPhone"]})


def test_wifi_networks_can_be_removed(tmp_path):
    path = tmp_path / "wifi.json"
    from player_app.wifi_networks import load_networks, remove_network, save_network
    save_network("Mitera", "secret", path)

    async def scenario():
        client, _ = await _make_client()
        try:
            with patch("web.web_service.remove_network", side_effect=lambda s: remove_network(s, path)):
                bad = await client.delete("/api/wifi/networks", json={})
                ok = await client.delete("/api/wifi/networks", json={"ssid": "Mitera"})
                gone = await client.delete("/api/wifi/networks", json={"ssid": "Mitera"})
                return bad.status, ok.status, gone.status
        finally:
            await client.close()

    assert asyncio.run(scenario()) == (400, 200, 404)
    assert load_networks(path) == []


def test_network_tab_shows_the_current_connection_and_saved_networks():
    from web import system_info

    def nmcli(command, *args, **kwargs):
        line = " ".join(command)
        if "--active" in line:
            return "Mitera-profile:802-11-wireless:wlan0\nWired connection 1:802-3-ethernet:eth0\n"
        if "802-11-wireless.ssid" in line:
            return "Mitera\ninfrastructure\n"
        if "ACTIVE,SIGNAL" in line:
            return "no:30\nyes:72\n"
        if "SIGNAL,SSID" in line:
            return "72:Mitera\n40:Neighbor\n"
        if command[:2] == ["ip", "-4"]:
            return "2: wlan0    inet 192.168.86.57/24 brd x\n"
        return ""

    with patch.object(system_info, "_run", side_effect=lambda *c, **k: nmcli(list(c))), \
            patch.object(system_info, "saved_wifi_names", return_value=["Mitera", "AV-iPhone17Pro"]):
        data = system_info.network()
    assert data["wifi"] == {"ssid": "Mitera", "mode": "client", "signal": 72}
    assert data["addresses"] == [{"interface": "wlan0", "address": "192.168.86.57"}]
    assert data["saved_networks"] == [{"ssid": "Mitera", "connected": True}, {"ssid": "AV-iPhone17Pro", "connected": False}]

    hotspot = {"--active": "magicboxie-hotspot:802-11-wireless:wlan0\n", "802-11-wireless.ssid": "MagicBoxie Player\nap\n"}
    with patch.object(system_info, "_run", side_effect=lambda *c, **k: next((v for key, v in hotspot.items() if key in " ".join(c)), "")):
        assert system_info.current_wifi() == {"ssid": "MagicBoxie Player", "mode": "hotspot", "signal": None}

    async def scenario():
        client, _ = await _make_client()
        try:
            with patch("web.web_service.system_info.network", return_value={"wifi": None}), \
                    patch("web.web_service.internet_reachable", new=AsyncMock(return_value=False)):
                return await (await client.get("/api/network")).json()
        finally:
            await client.close()

    assert asyncio.run(scenario()) == {"wifi": None, "internet_reachable": False}


def test_activity_reports_downloads_and_the_home_servers_progress(tmp_path):
    from player_app.services.home_sync_service import SyncActivity

    async def scenario():
        library = FakeLibrary(transcode_dir=tmp_path)
        client, controller = await _make_client(library)
        controller.currently_syncing_movie_title = "Gamma"
        controller.sync_activity = SyncActivity(
            queued=["Delta", "Epsilon"], bytes_done=50, bytes_total=200,
            preparing=[{"title": "Zeta", "status": "transcoding", "progress_percent": 40.0}],
            reached_at=1.0, reachable=True,
        )
        try:
            resp = await client.get("/api/activity")
            assert resp.status == 200
            return await resp.json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data["downloading"] == {"title": "Gamma", "bytes_done": 50, "bytes_total": 200}
    assert data["download_queue"] == ["Delta", "Epsilon"]
    assert data["home_server"]["preparing"] == [{"title": "Zeta", "status": "transcoding", "progress_percent": 40.0}]
    assert data["paused_for_playback"] is False


def test_activity_is_empty_when_nothing_is_happening(tmp_path):
    async def scenario():
        library = FakeLibrary(transcode_dir=tmp_path)
        (tmp_path / "0.mp4").write_bytes(b"x")
        (tmp_path / "1.mp4").write_bytes(b"x")
        client, _ = await _make_client(library)
        try:
            return await (await client.get("/api/activity")).json()
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert data == {
        "downloading": None, "download_queue": [], "transcoding": None,
        "home_server": None, "paused_for_playback": False,
    }
