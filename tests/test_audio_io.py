"""Tests de ``cli.audio_io.StreamingAudioPlayer`` sin hardware real.

``sd.RawOutputStream`` se reemplaza por un fake en memoria: la lógica de cola
(``write``/``interrupt``, contadores de diagnostico) es pura y no necesita
tocar audio de verdad para verificarse.
"""

from __future__ import annotations

import time

from gemini_live_demo.cli import audio_io


class _FakeRawOutputStream:
    """Fake de ``sd.RawOutputStream``: context manager que registra writes."""

    instances: list[_FakeRawOutputStream] = []

    def __init__(self, samplerate, channels, dtype, blocksize) -> None:  # noqa: ANN001
        self.samplerate = samplerate
        self.channels = channels
        self.written_chunks: list[bytes] = []
        _FakeRawOutputStream.instances.append(self)

    def __enter__(self) -> _FakeRawOutputStream:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def write(self, chunk: bytes) -> None:
        self.written_chunks.append(chunk)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condicion no se cumplio a tiempo')


def test_streaming_player_reproduce_chunks_encolados(monkeypatch):
    _FakeRawOutputStream.instances.clear()
    monkeypatch.setattr(audio_io.sd, 'RawOutputStream', _FakeRawOutputStream)

    player = audio_io.StreamingAudioPlayer(sample_rate=24000, channels=1)
    try:
        player.write(b'\x00\x01' * 100)
        _wait_until(lambda: _FakeRawOutputStream.instances and _FakeRawOutputStream.instances[0].written_chunks)
    finally:
        player.close()

    assert _FakeRawOutputStream.instances[0].written_chunks == [b'\x00\x01' * 100]
    assert player.received_audio_ms > 0


def test_streaming_player_interrupt_descarta_cola_pendiente(monkeypatch):
    _FakeRawOutputStream.instances.clear()
    monkeypatch.setattr(audio_io.sd, 'RawOutputStream', _FakeRawOutputStream)

    player = audio_io.StreamingAudioPlayer(sample_rate=24000, channels=1)
    try:
        # Encola varios chunks sin dejar que el thread los consuma todavia.
        for _ in range(5):
            player._queue.put(b'\x00\x00' * 10)
        player.interrupt()
        assert player.interrupted_dropped_chunks >= 1
    finally:
        player.close()


def test_streaming_player_disabled_no_abre_stream(monkeypatch):
    _FakeRawOutputStream.instances.clear()
    monkeypatch.setattr(audio_io.sd, 'RawOutputStream', _FakeRawOutputStream)

    player = audio_io.StreamingAudioPlayer(sample_rate=24000, channels=1, enabled=False)
    try:
        player.write(b'\x00\x01' * 100)
        time.sleep(0.05)
    finally:
        player.close()

    assert _FakeRawOutputStream.instances == []
    assert player.received_audio_ms > 0


def test_streaming_player_close_es_idempotente(monkeypatch):
    monkeypatch.setattr(audio_io.sd, 'RawOutputStream', _FakeRawOutputStream)

    player = audio_io.StreamingAudioPlayer(sample_rate=24000, channels=1)
    player.close()
    player.close()  # no debe lanzar
