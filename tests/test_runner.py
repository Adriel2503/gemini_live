"""Tests de los helpers de turno del modo continuo (``cli.runner``).

``_finalize_turn`` y ``_note_vad_signal`` se extrajeron de
``DemoRunner._continuous_loop`` para que fueran testeables sin sesion, audio
ni red: reciben dataclasses de stats como parametros y no dependen de
``self``. Este archivo confirma que el comportamiento se preservo intacto.

Ejecutable con pytest o directamente (``python tests/test_runner.py``).
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

from gemini_live_demo.cli.runner import (
    ContinuousSessionStats,
    DroppedChunksStats,
    QueueStats,
    TurnState,
    _finalize_turn,
    _note_vad_signal,
)
from gemini_live_demo.core.events import summarize_event
from gemini_live_demo.core.metrics import MetricsCsv, StreamStats, VadStats


def _settings(**overrides):
    base = dict(
        model='gemini-2.5-flash-native-audio-latest',
        language='es-US',
        voice_name='Aoede',
        chunk_ms=20,
        continuous_vad_silence_ms=700,
        continuous_vad_prefix_ms=200,
        metrics_csv_enabled=False,
        metrics_dir='metrics',
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _player(**overrides):
    base = dict(
        started_at=0.0,
        max_queue_size=3,
        interrupted_dropped_chunks=0,
        received_audio_ms=100.0,
        written_audio_ms=80.0,
        stream_sample_rate=24000.0,
        last_chunk_ms=20.0,
        write_max_ms=5.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stats(**overrides):
    base = dict(
        dropped=DroppedChunksStats(),
        queue=QueueStats(max_size=4),
        stream=StreamStats(
            model_speaking=True,
            send_chunks=10,
            send_bytes=6400,
            send_total_ms=50.0,
            send_max_ms=8.0,
            send_over_budget_count=0,
            send_while_model_speaking_chunks=0,
        ),
        vad=VadStats(),
    )
    base.update(overrides)
    return ContinuousSessionStats(**base)


def _kwargs(**overrides):
    kwargs = dict(
        settings=_settings(),
        turn_index=0,
        now=10.0,
        summary=summarize_event({'server_content': {'turn_complete': True}}),
        turn_state=TurnState(),
        stats=_stats(),
        player=_player(),
        session_id='sess1',
        metrics=MetricsCsv(_settings()),
        audio_queue=queue.Queue(),
    )
    kwargs.update(overrides)
    return kwargs


def test_empty_turn_marker_is_ignored():
    """Un turn_complete sin texto/audio/interrupcion no cuenta como turno."""
    kwargs = _kwargs(stats=_stats(queue=QueueStats(max_size=9)))
    turn_index = _finalize_turn(**kwargs)
    assert turn_index == 0  # no incrementa
    assert kwargs['stats'].queue.max_size == 9  # tampoco resetea el backlog


def test_turn_with_text_increments_and_resets_queue_backlog():
    turn_state = TurnState(text_parts=['hola'], turn_started_at=9.0)
    audio_queue: queue.Queue = queue.Queue()
    audio_queue.put(b'x')  # qsize() vale 1 al momento de cerrar el turno

    kwargs = _kwargs(
        turn_index=5,
        turn_state=turn_state,
        audio_queue=audio_queue,
        stats=_stats(queue=QueueStats(max_size=40)),
    )
    turn_index = _finalize_turn(**kwargs)

    assert turn_index == 6
    # al cerrar un turno CON contenido, el backlog se resetea al tamano actual
    # de la cola (no queda pegado al pico historico de 40).
    assert kwargs['stats'].queue.max_size == 1


def test_turn_with_only_interruption_still_counts():
    turn_state = TurnState(interrupted_seen=True)
    turn_index = _finalize_turn(**_kwargs(turn_state=turn_state))
    assert turn_index == 1


def test_first_audio_latency_none_when_no_audio_seen():
    turn_state = TurnState(text_parts=['hola'], turn_started_at=5.0, first_audio_at=None)
    row_written = {}

    class _RecordingMetrics(MetricsCsv):
        def write_row(self, row):  # noqa: ANN001
            row_written.update(row)  # metrics_csv_enabled=False: nunca toca disco

    _finalize_turn(**_kwargs(turn_state=turn_state, metrics=_RecordingMetrics(_settings()), now=6.0))
    assert row_written['first_audio_latency_ms'] is None


def test_note_vad_signal_counts_start_and_end():
    vad_stats = VadStats()
    start = summarize_event({'voiceActivity': {'voiceActivityType': 'START', 'audioOffset': '10'}})
    end = summarize_event({'voiceActivity': {'voiceActivityType': 'END', 'audioOffset': '20'}})

    _note_vad_signal(start, vad_stats)
    _note_vad_signal(end, vad_stats)

    assert vad_stats == VadStats(start_count=1, end_count=1, last_type='END', last_offset='20')


def test_note_vad_signal_noop_without_activity():
    vad_stats = VadStats()
    _note_vad_signal(summarize_event({}), vad_stats)
    assert vad_stats == VadStats()


def _main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith('test_') and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except AssertionError as exc:
            failures += 1
            print(f'FAIL {name}: {exc!r}')
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f'ERROR {name}: {exc!r}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    import sys

    sys.exit(_main())
