"""Tests de ``Settings``: defaults puros y lectura de entorno aislada."""

from __future__ import annotations

import os
from contextlib import contextmanager

from gemini_live_demo.core.config import Settings


@contextmanager
def _env(**values):
    """Fija variables de entorno temporalmente y las restaura al salir."""
    previous = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_defaults_are_pure_no_env():
    """``Settings()`` no lee el entorno: da defaults deterministas."""
    with _env(GEMINI_MODEL='deberia-ser-ignorado', GEMINI_CHANNELS='7'):
        s = Settings()
    assert s.api_key is None
    assert s.model == 'gemini-2.5-flash-native-audio-latest'
    assert s.channels == 1
    assert s.continuous_mode is True


def test_from_env_reads_environment():
    """``from_env()`` sí lee el entorno y castea tipos correctamente."""
    with _env(
        GEMINI_API_KEY='secreta',
        GEMINI_MODEL='modelo-x',
        GEMINI_CHANNELS='2',
        GEMINI_CHUNK_MS='40',
        GEMINI_NO_PLAYBACK='true',
        GEMINI_MIN_RECORD_SECONDS='1.5',
    ):
        s = Settings.from_env()
    assert s.api_key == 'secreta'
    assert s.model == 'modelo-x'
    assert s.channels == 2 and isinstance(s.channels, int)
    assert s.chunk_ms == 40
    assert s.no_playback is True
    assert s.min_record_seconds == 1.5


def test_from_env_falls_back_to_defaults():
    """Sin variables, ``from_env()`` coincide con los defaults."""
    with _env(
        GEMINI_API_KEY=None,
        GEMINI_MODEL=None,
        GEMINI_CHANNELS=None,
        GEMINI_NO_PLAYBACK=None,
    ):
        s = Settings.from_env()
    assert s.model == Settings().model
    assert s.channels == Settings().channels
    assert s.no_playback is False


def test_frames_per_chunk():
    assert Settings(input_sample_rate=16000, chunk_ms=20).frames_per_chunk == 320
    assert Settings(input_sample_rate=24000, chunk_ms=10).frames_per_chunk == 240


def _main() -> int:
    tests = [
        test_defaults_are_pure_no_env,
        test_from_env_reads_environment,
        test_from_env_falls_back_to_defaults,
        test_frames_per_chunk,
    ]
    for test in tests:
        test()
    print(f'\n{len(tests)}/{len(tests)} passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
