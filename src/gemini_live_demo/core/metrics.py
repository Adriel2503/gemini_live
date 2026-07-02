"""Métricas de sesión para Gemini Live.

Se extrae del monolito ``app.py`` para poder validarlas de forma aislada:

- ``MetricsCsv``: escritura del CSV por sesión.
- ``build_metrics_row``: función pura que construye la fila de métricas a
  partir de valores primitivos. Al no depender de audio ni de red, se puede
  probar con eventos sintéticos (ver ``tests/test_metrics.py``).
"""

from __future__ import annotations

import csv
import logging
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger('gemini_live_demo')


# Orden de columnas del CSV. Es también el contrato que validan los tests:
# toda clave devuelta por ``build_metrics_row`` debe existir aquí.
METRICS_FIELDNAMES = [
    'session_id',
    'turn_index',
    'model',
    'language',
    'voice_name',
    'chunk_ms',
    'vad_silence_ms',
    'vad_prefix_ms',
    'events',
    'text_present',
    'audio_chunks',
    'audio_bytes',
    'generation_complete',
    'turn_complete',
    'interrupted',
    'first_audio_latency_ms',
    'turn_duration_ms',
    'max_queue_size',
    'max_queue_delay_ms',
    'dropped_chunks',
    'send_chunks',
    'send_bytes',
    'send_avg_ms',
    'send_max_ms',
    'send_over_budget_count',
    'send_while_model_speaking_chunks',
    'playback_max_queue_size',
    'playback_interrupted_dropped_chunks',
    'playback_received_audio_ms',
    'playback_written_audio_ms',
    'playback_queue_audio_ms',
    'playback_receive_rate_x',
    'playback_write_rate_x',
    'playback_stream_sample_rate',
    'playback_last_chunk_ms',
    'playback_write_max_ms',
    'vad_start_count',
    'vad_end_count',
    'vad_last_type',
    'vad_last_audio_offset',
    'created_at',
]


class MetricsCsv:
    fieldnames = METRICS_FIELDNAMES

    def __init__(self, settings: Any) -> None:
        self.enabled = settings.metrics_csv_enabled
        self.path: Path | None = None
        self._file = None
        self._writer: csv.DictWriter | None = None
        if not self.enabled:
            return
        metrics_dir = Path(settings.metrics_dir)
        metrics_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        self.path = metrics_dir / f'gemini_live_metrics_{stamp}.csv'
        self._file = self.path.open('w', newline='', encoding='utf-8')
        self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames)
        self._writer.writeheader()
        logger.info('[metrics] csv=%s', self.path)

    def write_row(self, row: dict[str, Any]) -> None:
        if self._writer is None:
            return
        self._writer.writerow(row)
        assert self._file is not None
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def build_metrics_row(
    *,
    settings: Any,
    session_id: str,
    turn_index: int,
    language: str,
    event_count: int,
    response_text: str,
    response_audio_chunks: int,
    response_audio_bytes: int,
    generation_complete: bool,
    turn_complete: bool,
    interrupted: bool,
    first_audio_latency_ms: int | None,
    turn_duration_ms: int,
    max_queue_size: int,
    dropped_chunks: int,
    stream_stats: dict[str, Any],
    vad_stats: dict[str, Any],
    player: Any,
    player_elapsed_ms: float,
    created_at: str,
) -> dict[str, Any]:
    """Construye la fila de métricas de un turno.

    Función pura: recibe valores/contadores ya calculados y devuelve el dict
    que se escribe en el CSV. ``player`` sólo se usa por sus atributos, así que
    en tests puede ser cualquier objeto con esos campos.
    """

    return {
        'session_id': session_id,
        'turn_index': turn_index,
        'model': settings.model,
        'language': language,
        'voice_name': settings.voice_name,
        'chunk_ms': settings.chunk_ms,
        'vad_silence_ms': settings.continuous_vad_silence_ms,
        'vad_prefix_ms': settings.continuous_vad_prefix_ms,
        'events': event_count,
        'text_present': bool(response_text),
        'audio_chunks': response_audio_chunks,
        'audio_bytes': response_audio_bytes,
        'generation_complete': generation_complete,
        'turn_complete': turn_complete,
        'interrupted': interrupted,
        'first_audio_latency_ms': first_audio_latency_ms,
        'turn_duration_ms': turn_duration_ms,
        'max_queue_size': max_queue_size,
        'max_queue_delay_ms': max_queue_size * settings.chunk_ms,
        'dropped_chunks': dropped_chunks,
        'send_chunks': stream_stats['send_chunks'],
        'send_bytes': stream_stats['send_bytes'],
        'send_avg_ms': round(stream_stats['send_total_ms'] / max(1, stream_stats['send_chunks']), 3),
        'send_max_ms': round(stream_stats['send_max_ms'], 3),
        'send_over_budget_count': stream_stats['send_over_budget_count'],
        'send_while_model_speaking_chunks': stream_stats['send_while_model_speaking_chunks'],
        'playback_max_queue_size': player.max_queue_size,
        'playback_interrupted_dropped_chunks': player.interrupted_dropped_chunks,
        'playback_received_audio_ms': round(player.received_audio_ms, 1),
        'playback_written_audio_ms': round(player.written_audio_ms, 1),
        'playback_queue_audio_ms': round(max(0.0, player.received_audio_ms - player.written_audio_ms), 1),
        'playback_receive_rate_x': round(player.received_audio_ms / max(1.0, player_elapsed_ms), 3),
        'playback_write_rate_x': round(player.written_audio_ms / max(1.0, player_elapsed_ms), 3),
        'playback_stream_sample_rate': player.stream_sample_rate,
        'playback_last_chunk_ms': round(player.last_chunk_ms, 3),
        'playback_write_max_ms': round(player.write_max_ms, 3),
        'vad_start_count': vad_stats['start_count'],
        'vad_end_count': vad_stats['end_count'],
        'vad_last_type': vad_stats['last_type'],
        'vad_last_audio_offset': vad_stats['last_offset'],
        'created_at': created_at,
    }
