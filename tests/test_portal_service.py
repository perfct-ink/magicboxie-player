import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from player_app import main


def test_http_serves_api_and_portal_and_cleans_up():
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        runner = MagicMock(setup=AsyncMock(), cleanup=AsyncMock())
        site = MagicMock(start=AsyncMock())
        with patch.object(main, "HTTP_PORT", 8000), patch.object(main, "PORTAL_PORT", 80), \
                patch("web.web_service.create_app", return_value=object()), \
                patch("aiohttp.web.AppRunner", return_value=runner), \
                patch("aiohttp.web.TCPSite", return_value=site) as make_site:
            await main._run_http(MagicMock(), stop)
            assert [call.args for call in make_site.call_args_list] == [
                (runner, "0.0.0.0", 8000), (runner, "0.0.0.0", 80)]
            assert site.start.await_count == 2
            runner.cleanup.assert_awaited_once()
    asyncio.run(scenario())


def test_portal_bind_failure_keeps_the_api_listener_up():
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        runner = MagicMock(setup=AsyncMock(), cleanup=AsyncMock())
        site = MagicMock(start=AsyncMock(side_effect=[None, OSError("port in use")]))
        with patch.object(main, "HTTP_PORT", 8000), patch.object(main, "PORTAL_PORT", 80), \
                patch("web.web_service.create_app", return_value=object()), \
                patch("aiohttp.web.AppRunner", return_value=runner), \
                patch("aiohttp.web.TCPSite", return_value=site):
            # Something else holding port 80 (e.g. nginx) must not take the
            # API on 8000 down with it: this runs until stopped, then cleans up.
            await main._run_http(MagicMock(), stop)
            runner.cleanup.assert_awaited_once()
    asyncio.run(scenario())
