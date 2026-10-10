"""Checks the SoC temperature once a minute. At an unsafe level (the Pi
starts throttling its clocks around here) a playing movie is paused and a
note is left for the web page and the idle screen; the note clears once the
device has cooled, and playback is never resumed on its own."""
from __future__ import annotations

import asyncio
import logging

from ..controllers.playback_controller import PlaybackController
from ..models.protocol import Command, Opcode, PlaybackStatus
from ..util import cpu_temperature_celsius, sleep_unless_stopped

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
UNSAFE_CELSIUS = 80.0
# Must cool this far before the note goes away (stops it flickering at the limit).
SAFE_AGAIN_CELSIUS = 70.0


class ThermalService:
    def __init__(self, controller: PlaybackController):
        self._controller = controller

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.check()
            except Exception:
                logger.exception("Temperature check failed")
            await sleep_unless_stopped(stop_event, CHECK_INTERVAL_SECONDS)

    async def check(self) -> None:
        temperature = cpu_temperature_celsius()
        if temperature is None:
            return
        controller = self._controller
        if temperature >= UNSAFE_CELSIUS:
            if controller.thermal_note is None:
                logger.warning("Device at %.0f°C (unsafe from %.0f°C)", temperature, UNSAFE_CELSIUS)
            controller.thermal_note = f"Too hot ({temperature:.0f}°C). Playback paused until it cools down."
            if not controller.is_idle:
                state = await controller.refresh_status()
                if state.status == PlaybackStatus.PLAYING:
                    logger.warning("Pausing the movie: device at %.0f°C", temperature)
                    await controller.handle_command(Command(Opcode.PAUSE))
                # Over the paused picture (the idle screen carries the note
                # itself); redrawn every check so a newly loaded movie has it.
                await controller.player.show_note(
                    f"Too hot ({temperature:.0f}°C) - paused. Let the device cool down.")
        elif temperature <= SAFE_AGAIN_CELSIUS and controller.thermal_note is not None:
            logger.info("Device cooled to %.0f°C", temperature)
            controller.thermal_note = None
            await controller.player.hide_note()
