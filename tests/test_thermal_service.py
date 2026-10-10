import asyncio

from player_app.models.protocol import Opcode, PlaybackState, PlaybackStatus
from player_app.services import thermal_service
from player_app.services.thermal_service import ThermalService


class FakePlayer:
    def __init__(self):
        self.note = None

    async def show_note(self, text):
        self.note = text

    async def hide_note(self):
        self.note = None


class FakeController:
    def __init__(self, idle=False, status=PlaybackStatus.PLAYING):
        self.thermal_note = None
        self.is_idle = idle
        self._status = status
        self.commands = []
        self.player = FakePlayer()

    async def refresh_status(self):
        return PlaybackState(status=self._status, movie_id=1, position_seconds=5)

    async def handle_command(self, cmd):
        self.commands.append(cmd.opcode)


def _check(controller, monkeypatch, temperature):
    monkeypatch.setattr(thermal_service, "cpu_temperature_celsius", lambda: temperature)
    asyncio.run(ThermalService(controller).check())


def test_pauses_a_playing_movie_and_leaves_a_note_when_too_hot(monkeypatch):
    controller = FakeController()
    _check(controller, monkeypatch, 82.4)
    assert controller.commands == [Opcode.PAUSE]
    assert "82" in controller.thermal_note
    assert "82" in controller.player.note and "paused" in controller.player.note


def test_note_stays_until_properly_cooled_and_nothing_resumes(monkeypatch):
    controller = FakeController(status=PlaybackStatus.PAUSED)
    _check(controller, monkeypatch, 85)
    _check(controller, monkeypatch, 75)
    assert controller.thermal_note is not None and controller.player.note is not None
    _check(controller, monkeypatch, 65)
    assert controller.thermal_note is None and controller.player.note is None
    assert controller.commands == []


def test_normal_temperature_or_missing_sensor_does_nothing(monkeypatch):
    controller = FakeController()
    _check(controller, monkeypatch, 55)
    _check(controller, monkeypatch, None)
    assert controller.commands == [] and controller.thermal_note is None
