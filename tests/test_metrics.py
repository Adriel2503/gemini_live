"""Validación de las métricas de sesión.

Prueba ``build_metrics_row`` y ``MetricsCsv`` con datos sintéticos, sin
necesitar micrófono, red ni Gemini en vivo. Ese era el objetivo original:
confirmar que las métricas se calculan y se escriben correctamente.

Ejecutable con pytest (``pytest tests/``) o directamente
(``python tests/test_metrics.py``) para no depender de pytest instalado.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

from gemini_live_demo.core.metrics import (
    METRICS_FIELDNAMES,
    MetricsCsv,
    StreamStats,
    VadStats,
    build_metrics_row,
)


def _settings(**overrides):
    base = dict(
        model='gemini-3.1-flash-live-preview',
        voice_name='Aoede',
        chunk_ms=20,
        continuous_vad_silence_ms=700,
        continuous_vad_prefix_ms=200,
        metrics_csv_enabled=True,
        metrics_dir='metrics',
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _player(**overrides):
    base = dict(
        max_queue_size=3,
        interrupted_dropped_chunks=1,
        received_audio_ms=1000.0,
        written_audio_ms=800.0,
        stream_sample_rate=24000.0,
        last_chunk_ms=20.0,
        write_max_ms=5.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stream_stats(**overrides):
    base = dict(
        send_chunks=10,
        send_bytes=6400,
        send_total_ms=50.0,
        send_max_ms=8.0,
        send_over_budget_count=2,
        send_while_model_speaking_chunks=1,
    )
    base.update(overrides)
    return StreamStats(**base)


def _vad_stats(**overrides):
    base = dict(start_count=2, end_count=2, last_type='END', last_offset='1234')
    base.update(overrides)
    return VadStats(**base)


def _row(**overrides):
    kwargs = dict(
        settings=_settings(),
        session_id='sess1',
        turn_index=1,
        language='es-US',
        event_count=5,
        response_text='hola',
        response_audio_chunks=4,
        response_audio_bytes=2048,
        generation_complete=True,
        turn_complete=False,
        interrupted=False,
        first_audio_latency_ms=120,
        turn_duration_ms=1500,
        max_queue_size=6,
        dropped_chunks=0,
        stream_stats=_stream_stats(),
        vad_stats=_vad_stats(),
        player=_player(),
        player_elapsed_ms=2000.0,
        created_at='2026-07-01T10:00:00',
    )
    kwargs.update(overrides)
    return build_metrics_row(**kwargs)


def test_row_keys_match_csv_contract():
    """La fila debe cubrir exactamente las columnas del CSV: ni de más ni de menos."""
    row = _row()
    assert set(row.keys()) == set(METRICS_FIELDNAMES)


def test_derived_values_are_correct():
    row = _row()
    # send_avg_ms = send_total_ms / send_chunks = 50 / 10
    assert row['send_avg_ms'] == 5.0
    # max_queue_delay_ms = max_queue_size * chunk_ms = 6 * 20
    assert row['max_queue_delay_ms'] == 120
    # playback_queue_audio_ms = received - written = 1000 - 800
    assert row['playback_queue_audio_ms'] == 200.0
    # rates = audio_ms / elapsed_ms
    assert row['playback_receive_rate_x'] == round(1000.0 / 2000.0, 3)
    assert row['playback_write_rate_x'] == round(800.0 / 2000.0, 3)
    # text_present derivado de response_text no vacío
    assert row['text_present'] is True
    assert row['vad_last_type'] == 'END'


def test_zero_send_chunks_does_not_divide_by_zero():
    row = _row(stream_stats=_stream_stats(send_chunks=0, send_total_ms=0.0))
    assert row['send_avg_ms'] == 0.0
    assert row['send_chunks'] == 0


def test_zero_elapsed_does_not_divide_by_zero():
    row = _row(player_elapsed_ms=0.0)
    # max(1.0, elapsed) evita división por cero
    assert row['playback_receive_rate_x'] == round(1000.0 / 1.0, 3)


def test_first_audio_latency_none_passes_through():
    row = _row(first_audio_latency_ms=None)
    assert row['first_audio_latency_ms'] is None


def test_empty_text_marks_text_present_false():
    row = _row(response_text='')
    assert row['text_present'] is False


def test_metrics_csv_disabled_is_noop(tmp_path):
    csvw = MetricsCsv(_settings(metrics_csv_enabled=False))
    csvw.write_row(_row())  # no debe fallar ni crear archivo
    csvw.close()
    assert csvw.path is None


def test_metrics_csv_writes_header_and_row(tmp_path):
    settings = _settings(metrics_dir=str(tmp_path))
    csvw = MetricsCsv(settings)
    csvw.write_row(_row(session_id='sessX', turn_index=7))
    csvw.close()

    files = list(Path(tmp_path).glob('gemini_live_metrics_*.csv'))
    assert len(files) == 1
    with files[0].open(newline='', encoding='utf-8') as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == METRICS_FIELDNAMES
    assert rows[0]['session_id'] == 'sessX'
    assert rows[0]['turn_index'] == '7'


# --- Runner sin pytest -------------------------------------------------------

def _main() -> int:
    import inspect
    import tempfile

    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith('test_') and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            if 'tmp_path' in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
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
