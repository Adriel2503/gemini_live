"""Tests de los comandos Typer de ``cli.app`` (traduccion de flags -> Settings).

``DemoRunner.run`` se monkeypatchea a un no-op: estos tests verifican que
``run()`` arma la ``Settings``/``GeminiLiveAdapter`` correctos segun los
flags, no el bucle continuo en si (ya cubierto por ``test_runner.py``).
"""

from __future__ import annotations

from typer.testing import CliRunner

from gemini_live_demo.cli import app as cli_app
from gemini_live_demo.cli.runner import DemoRunner

runner = CliRunner()


def _patch_demo_runner(monkeypatch):
    captured = {}

    def fake_init(self, adapter, settings):
        captured['adapter'] = adapter
        captured['settings'] = settings

    async def fake_run(self):
        return None

    monkeypatch.setattr(DemoRunner, '__init__', fake_init)
    monkeypatch.setattr(DemoRunner, 'run', fake_run)
    return captured


def test_run_sin_api_key_ni_mock_falla(monkeypatch):
    # setenv('', ...) en vez de delenv: load_dotenv() no pisa una var ya
    # presente en el entorno (aunque este vacia), pero si repuebla una
    # ausente desde el .env local si existe uno con GEMINI_API_KEY real.
    monkeypatch.setenv('GEMINI_API_KEY', '')
    _patch_demo_runner(monkeypatch)

    result = runner.invoke(cli_app.app, ['run'])

    assert result.exit_code != 0


def test_run_mock_no_requiere_api_key(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    captured = _patch_demo_runner(monkeypatch)

    result = runner.invoke(cli_app.app, ['run', '--mock'])

    assert result.exit_code == 0
    assert captured['settings'].continuous_mode is True


def test_run_manual_desactiva_continuous_mode(monkeypatch):
    captured = _patch_demo_runner(monkeypatch)

    result = runner.invoke(cli_app.app, ['run', '--mock', '--manual'])

    assert result.exit_code == 0
    assert captured['settings'].continuous_mode is False


def test_run_no_playback_setea_no_playback(monkeypatch):
    captured = _patch_demo_runner(monkeypatch)

    result = runner.invoke(cli_app.app, ['run', '--mock', '--no-playback'])

    assert result.exit_code == 0
    assert captured['settings'].no_playback is True


def test_run_record_output_wav_implica_no_playback(monkeypatch):
    captured = _patch_demo_runner(monkeypatch)

    result = runner.invoke(cli_app.app, ['run', '--mock', '--record-output-wav'])

    assert result.exit_code == 0
    assert captured['settings'].record_output_wav is True
    assert captured['settings'].no_playback is True


def test_list_devices_cmd_delega_en_list_devices(monkeypatch):
    called = {'value': False}

    def fake_list_devices():
        called['value'] = True

    monkeypatch.setattr(cli_app, 'list_devices', fake_list_devices)

    result = runner.invoke(cli_app.app, ['list-devices'])

    assert result.exit_code == 0
    assert called['value'] is True
