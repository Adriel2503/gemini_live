"""Orquestacion de la conversacion en vivo.

``DemoRunner`` coordina sesion, microfono, reproductor y metricas en dos
modos: streaming continuo (VAD del lado del modelo) y manual (Enter para
grabar/enviar). Es la capa de integracion que une todas las demas.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.panel import Panel

from gemini_live_demo.cli.audio_io import StreamingAudioPlayer, capture_until_enter, play_pcm
from gemini_live_demo.core.audio import ensure_mono, float32_to_int16, normalize_language_code
from gemini_live_demo.core.config import Settings
from gemini_live_demo.core.events import EventSummary, summarize_event
from gemini_live_demo.core.metrics import MetricsCsv, StreamStats, VadStats, build_metrics_row
from gemini_live_demo.core.session import GeminiLiveAdapter

console = Console()
logger = logging.getLogger('gemini_live_demo')


@dataclass
class TurnState:
    """Acumuladores de un turno en curso (modo continuo). Se resetea por turno."""

    event_count: int = 0
    response_audio_chunks: int = 0
    response_audio_bytes: int = 0
    text_parts: list[str] = field(default_factory=list)
    turn_started_at: float | None = None
    first_audio_at: float | None = None
    interrupted_seen: bool = False


@dataclass
class DroppedChunksStats:
    """Chunks de microfono descartados por cola llena (backpressure)."""

    count: int = 0


@dataclass
class QueueStats:
    """Pico historico de la cola de audio saliente hacia Gemini."""

    max_size: int = 0


@dataclass
class ContinuousSessionStats:
    """Agrupa las 4 stats que viven toda la sesion del modo continuo.

    Se arma una vez al abrir ``_continuous_loop`` y se pasa por referencia a
    las dos tasks concurrentes (``send_microphone``/``receive_model``) y al
    callback del microfono; cada uno muta solo los campos que le corresponden.
    """

    dropped: DroppedChunksStats = field(default_factory=DroppedChunksStats)
    queue: QueueStats = field(default_factory=QueueStats)
    stream: StreamStats = field(default_factory=StreamStats)
    vad: VadStats = field(default_factory=VadStats)


def _note_vad_signal(summary: EventSummary, vad_stats: VadStats) -> None:
    """Actualiza los contadores de VAD (in-place) a partir de un evento."""
    vad_type = summary.voice_activity_type or summary.vad_signal_type
    if not vad_type:
        return
    vad_stats.last_type = vad_type
    vad_stats.last_offset = summary.voice_activity_offset
    if 'START' in vad_type or 'SOS' in vad_type:
        vad_stats.start_count += 1
    if 'END' in vad_type or 'EOS' in vad_type:
        vad_stats.end_count += 1
    logger.debug('[vad] type=%s offset=%s', vad_type, summary.voice_activity_offset)


def _finalize_turn(
    *,
    settings: Settings,
    turn_index: int,
    now: float,
    summary: EventSummary,
    turn_state: TurnState,
    stats: ContinuousSessionStats,
    player: StreamingAudioPlayer,
    session_id: str,
    metrics: MetricsCsv,
    audio_queue: queue.Queue,
) -> int:
    """Cierra un turno: logea, escribe la fila de métricas y devuelve el próximo turn_index.

    Si el marcador de fin de turno no trae contenido (ni texto, ni audio, ni
    interrupción) se considera vacío y se ignora sin incrementar turn_index
    (y sin resetear ``stats.queue.max_size``: el backlog sigue siendo el
    de la conversación en curso, no el de un marcador vacío).
    """
    response_text = ''.join(turn_state.text_parts).strip()
    has_turn_payload = bool(
        response_text or turn_state.response_audio_chunks or turn_state.interrupted_seen
    )
    if not has_turn_payload:
        logger.info(
            '[live] ignored empty turn marker generation_complete=%s turn_complete=%s',
            summary.generation_complete,
            summary.turn_complete,
        )
        return turn_index

    turn_index += 1
    turn_started_at = turn_state.turn_started_at
    first_audio_at = turn_state.first_audio_at
    elapsed_ms = int((now - turn_started_at) * 1000) if turn_started_at is not None else 0
    first_audio_latency_ms = (
        int((first_audio_at - turn_started_at) * 1000)
        if first_audio_at is not None and turn_started_at is not None
        else None
    )
    max_queue_size = stats.queue.max_size
    logger.info(
        '[live] turn done events=%d text=%s audio_chunks=%d audio_bytes=%d first_audio_latency_ms=%s duration_ms=%d max_queue_size=%d dropped_chunks=%d generation_complete=%s turn_complete=%s',
        turn_state.event_count,
        'yes' if response_text else 'no',
        turn_state.response_audio_chunks,
        turn_state.response_audio_bytes,
        first_audio_latency_ms if first_audio_latency_ms is not None else 'n/a',
        elapsed_ms,
        max_queue_size,
        stats.dropped.count,
        'yes' if summary.generation_complete else 'no',
        'yes' if summary.turn_complete else 'no',
    )
    metrics.write_row(
        build_metrics_row(
            settings=settings,
            session_id=session_id,
            turn_index=turn_index,
            language=normalize_language_code(settings.language, settings.model),
            event_count=turn_state.event_count,
            response_text=response_text,
            response_audio_chunks=turn_state.response_audio_chunks,
            response_audio_bytes=turn_state.response_audio_bytes,
            generation_complete=summary.generation_complete,
            turn_complete=summary.turn_complete,
            interrupted=turn_state.interrupted_seen,
            first_audio_latency_ms=first_audio_latency_ms,
            turn_duration_ms=elapsed_ms,
            max_queue_size=max_queue_size,
            dropped_chunks=stats.dropped.count,
            stream_stats=stats.stream,
            vad_stats=stats.vad,
            player=player,
            player_elapsed_ms=(time.perf_counter() - player.started_at) * 1000,
            created_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
        )
    )
    stats.stream.model_speaking = False
    if response_text:
        logger.info('[live] gemini_text=%s', response_text)
    stats.queue.max_size = audio_queue.qsize()
    return turn_index


class DemoRunner:
    def __init__(self, adapter: GeminiLiveAdapter, settings: Settings) -> None:
        self.adapter = adapter
        self.settings = settings
        self._session_cm: Any | None = None
        self._session: Any | None = None

    async def _open_session(self) -> None:
        self._session_cm = await self.adapter.connect()
        self._session = await self._session_cm.__aenter__()
        self.adapter.clear_refresh_request()
        await self.adapter.greet(self._session)
        logger.info('[session] ready')

    async def _close_session(self) -> None:
        if self._session_cm is None:
            return
        with suppress(Exception):
            await self._session_cm.__aexit__(None, None, None)
        self._session_cm = None
        self._session = None

    async def _reconnect_session(self, reason: str) -> None:
        logger.warning('[session] reconnecting reason=%s', reason)
        await self._close_session()
        await self._open_session()

    async def _send_turn(self, turn_index: int, text: str, pcm: bytes | None) -> None:
        assert self._session is not None
        for attempt in (1, 2):
            try:
                if text:
                    await self.adapter.send_text(self._session, text)
                else:
                    assert pcm is not None
                    await self.adapter.send_audio(self._session, pcm)
                return
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.warning('[turn %d] send failed on attempt %d: %s', turn_index, attempt, exc)
                await self._reconnect_session('send_failure')
                assert self._session is not None

    async def run(self) -> None:
        await self._open_session()
        try:
            if self.settings.continuous_mode:
                await self._continuous_loop()
            else:
                await self._loop()
        finally:
            await self._close_session()

    async def _continuous_loop(self) -> None:
        assert self._session is not None
        console.print(
            Panel.fit(
                'Modo continuo. Presiona Enter una vez para iniciar. Habla normal; Gemini detecta fin de turno e interrupciones. Ctrl+C para salir.',
                title='Gemini Live',
            )
        )
        try:
            input('Presiona Enter para iniciar la conversacion...')
        except (EOFError, KeyboardInterrupt):
            return

        stop_event = asyncio.Event()
        audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=self.settings.audio_queue_max_chunks)
        stats = ContinuousSessionStats()
        session_id = time.strftime('%Y%m%d_%H%M%S')
        metrics = MetricsCsv(self.settings)
        output_wav_path = None
        if self.settings.record_output_wav:
            output_wav_path = Path(self.settings.output_wav_dir) / f'gemini_output_{session_id}.wav'
        player = StreamingAudioPlayer(
            self.settings.output_sample_rate,
            self.settings.channels,
            enabled=not self.settings.no_playback and not self.settings.record_output_wav,
            wav_path=output_wav_path,
        )

        def callback(indata, frames_count, time_info, status):  # noqa: ANN001
            if status:
                logger.warning('[mic] %s', status)
            pcm = float32_to_int16(np.asarray(indata, dtype=np.float32).copy())
            pcm = ensure_mono(pcm)
            try:
                audio_queue.put_nowait(np.asarray(pcm, dtype=np.int16).tobytes())
            except queue.Full:
                stats.dropped.count += 1

        async def send_microphone() -> None:
            sent_chunks = 0
            logger.info(
                '[mic] continuous stream opened rate=%dHz chunk_ms=%d queue_max_chunks=%d',
                self.settings.input_sample_rate,
                self.settings.chunk_ms,
                self.settings.audio_queue_max_chunks,
            )
            with sd.InputStream(
                samplerate=self.settings.input_sample_rate,
                channels=self.settings.channels,
                dtype='float32',
                blocksize=self.settings.frames_per_chunk,
                callback=callback,
            ):
                while not stop_event.is_set():
                    try:
                        chunk = await asyncio.to_thread(audio_queue.get, True, 0.1)
                    except queue.Empty:
                        continue
                    assert self._session is not None
                    send_started = time.perf_counter()
                    await self.adapter.send_audio_chunk(self._session, chunk)
                    send_ms = (time.perf_counter() - send_started) * 1000
                    sent_chunks += 1
                    stats.stream.send_chunks += 1
                    stats.stream.send_bytes += len(chunk)
                    stats.stream.send_total_ms += send_ms
                    stats.stream.send_max_ms = max(stats.stream.send_max_ms, send_ms)
                    if send_ms > self.settings.chunk_ms:
                        stats.stream.send_over_budget_count += 1
                    if stats.stream.model_speaking:
                        stats.stream.send_while_model_speaking_chunks += 1
                    queue_size = audio_queue.qsize()
                    stats.queue.max_size = max(stats.queue.max_size, queue_size)
                    queue_delay_ms = queue_size * self.settings.chunk_ms
                    if queue_size >= self.settings.audio_queue_warn_chunks:
                        logger.debug(
                            '[mic] queue backlog queue_size=%d queue_delay_ms=%d dropped_chunks=%d',
                            queue_size,
                            queue_delay_ms,
                            stats.dropped.count,
                        )
                    if sent_chunks % self.settings.audio_queue_log_every_chunks == 0:
                        logger.debug(
                            '[mic] streaming chunks_sent=%d queue_size=%d queue_delay_ms=%d dropped_chunks=%d send_avg_ms=%.2f send_max_ms=%.2f send_over_budget=%d model_speaking=%s',
                            sent_chunks,
                            queue_size,
                            queue_delay_ms,
                            stats.dropped.count,
                            stats.stream.send_total_ms / max(1, stats.stream.send_chunks),
                            stats.stream.send_max_ms,
                            stats.stream.send_over_budget_count,
                            'yes' if stats.stream.model_speaking else 'no',
                        )
            logger.info('[mic] continuous stream closed chunks_sent=%d dropped_chunks=%d', sent_chunks, stats.dropped.count)

        async def receive_model() -> None:
            turn_index = 0
            turn_state = TurnState()
            while not stop_event.is_set():
                assert self._session is not None
                saw_event = False
                async for event in self.adapter.receive(self._session):
                    saw_event = True
                    if stop_event.is_set():
                        break
                    now = time.perf_counter()
                    turn_state.event_count += 1
                    if turn_state.turn_started_at is None:
                        turn_state.turn_started_at = now
                    summary = summarize_event(event)
                    self.adapter.note_event(summary)
                    _note_vad_signal(summary, stats.vad)
                    if summary.interrupted:
                        turn_state.interrupted_seen = True
                        stats.stream.model_speaking = False
                        logger.warning('[live] interrupted=true')
                        player.interrupt()
                    if summary.text:
                        turn_state.text_parts.append(summary.text)
                    if summary.audio_chunks:
                        stats.stream.model_speaking = True
                        if turn_state.first_audio_at is None:
                            turn_state.first_audio_at = now
                        for chunk in summary.audio_chunks:
                            turn_state.response_audio_chunks += 1
                            turn_state.response_audio_bytes += len(chunk)
                            player.write(chunk)
                    if summary.done:
                        turn_index = _finalize_turn(
                            settings=self.settings,
                            turn_index=turn_index,
                            now=now,
                            summary=summary,
                            turn_state=turn_state,
                            stats=stats,
                            player=player,
                            session_id=session_id,
                            metrics=metrics,
                            audio_queue=audio_queue,
                        )
                        turn_state = TurnState()
                    if summary.go_away:
                        stop_event.set()
                        break
                if not saw_event:
                    await asyncio.sleep(0.05)

        def stop_on_task_failure(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning('[live] task failed name=%s error=%s', task.get_name(), exc)
                stop_event.set()

        sender = asyncio.create_task(send_microphone(), name='gemini-mic-sender')
        receiver = asyncio.create_task(receive_model(), name='gemini-receiver')
        sender.add_done_callback(stop_on_task_failure)
        receiver.add_done_callback(stop_on_task_failure)
        try:
            await receiver
        except KeyboardInterrupt:
            stop_event.set()
        except Exception as exc:
            logger.warning('[live] receive failed: %s', exc)
            stop_event.set()
        finally:
            stop_event.set()
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender
            player.close()
            metrics.close()

    async def _loop(self) -> None:
        console.print(
            Panel.fit(
                'Demo de voz iniciada. Enter para grabar y Enter otra vez para enviar. /exit para salir.',
                title='Gemini Live',
            )
        )
        turn_index = 0
        while True:
            if self.adapter.should_refresh_session:
                await self._reconnect_session('go_away')

            try:
                text = input('\nMensaje opcional (Enter para grabar voz): ').strip()
            except (EOFError, KeyboardInterrupt):
                break

            if text in {'/exit', 'exit', 'quit'}:
                logger.info('[turn %d] exit requested', turn_index + 1)
                break

            turn_index += 1
            started = time.perf_counter()
            response_audio: list[bytes] = []
            response_text_parts: list[str] = []
            model_turn_seen = False
            generation_complete_seen = False
            turn_complete_seen = False
            go_away_seen = False

            pcm: bytes | None = None
            if text:
                logger.info('[turn %d] mode=text chars=%d', turn_index, len(text))
            else:
                logger.info('[turn %d] mode=voice waiting for microphone', turn_index)
                console.print('Grabando...')
                pcm = await asyncio.to_thread(capture_until_enter, self.settings)
                if not pcm:
                    logger.warning('[turn %d] skipped because no usable audio was captured', turn_index)
                    continue

            await self._send_turn(turn_index, text, pcm)

            event_count = 0
            try:
                assert self._session is not None
                async for event in self.adapter.receive(self._session):
                    event_count += 1
                    summary = summarize_event(event)
                    self.adapter.note_event(summary)
                    model_turn_seen = model_turn_seen or summary.model_turn_present
                    generation_complete_seen = generation_complete_seen or summary.generation_complete
                    turn_complete_seen = turn_complete_seen or summary.turn_complete
                    go_away_seen = go_away_seen or summary.go_away
                    if summary.interrupted:
                        logger.warning('[turn %d] interrupted=true', turn_index)
                    if summary.generation_complete:
                        logger.info('[turn %d] generation_complete=true', turn_index)
                    if summary.turn_complete:
                        logger.info('[turn %d] turn_complete=true', turn_index)
                    if summary.text:
                        response_text_parts.append(summary.text)
                    if summary.audio_chunks:
                        response_audio.extend(summary.audio_chunks)
                    if summary.done:
                        break
            except Exception:
                logger.exception('[turn %d] receive failed', turn_index)
                await self._reconnect_session('receive_failure')
                continue

            response_text = ''.join(response_text_parts).strip()
            response_bytes = sum(len(chunk) for chunk in response_audio)
            logger.info(
                '[turn %d] response events=%d text=%s audio_chunks=%d audio_bytes=%d model_turn=%s generation_complete=%s turn_complete=%s go_away=%s',
                turn_index,
                event_count,
                'yes' if response_text else 'no',
                len(response_audio),
                response_bytes,
                'yes' if model_turn_seen else 'no',
                'yes' if generation_complete_seen else 'no',
                'yes' if turn_complete_seen else 'no',
                'yes' if go_away_seen else 'no',
            )
            if response_text:
                logger.info('[turn %d] gemini_text=%s', turn_index, response_text)
            if response_audio:
                try:
                    await asyncio.to_thread(play_pcm, b''.join(response_audio), self.settings.output_sample_rate)
                except Exception as exc:
                    logger.warning('[turn %d] playback failed: %s', turn_index, exc)
            else:
                logger.warning('[turn %d] Gemini returned no audio this turn', turn_index)

            logger.info('[turn %d] done in %.0f ms', turn_index, (time.perf_counter() - started) * 1000)
