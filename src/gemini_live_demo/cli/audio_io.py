"""Entrada/salida de audio con hardware (sounddevice) e hilos.

Aqui vive todo lo que toca el microfono, los altavoces o el disco: captura
bloqueante, reproduccion, listado de dispositivos y el reproductor en
streaming con cola. Es la capa con efectos secundarios; la logica pura de
DSP esta en ``audio.py``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from contextlib import suppress
from pathlib import Path

import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.table import Table

from gemini_live_demo.core.audio import (
    ensure_mono,
    float32_to_int16,
    int16_to_float32,
    resample_audio,
)
from gemini_live_demo.core.config import Settings

console = Console()
logger = logging.getLogger('gemini_live_demo')


def list_devices() -> None:
    devices = sd.query_devices()
    default_input, default_output = sd.default.device

    table = Table(title='Dispositivos de audio')
    table.add_column('#', justify='right')
    table.add_column('Nombre')
    table.add_column('Inputs', justify='right')
    table.add_column('Outputs', justify='right')
    table.add_column('Default', justify='center')

    for idx, device in enumerate(devices):
        table.add_row(
            str(idx),
            str(device['name']),
            str(device['max_input_channels']),
            str(device['max_output_channels']),
            'in/out' if idx in {default_input, default_output} else '',
        )

    console.print(table)


def capture_until_enter(settings: Settings) -> bytes:
    chunks: list[np.ndarray] = []
    stop = {'value': False}
    started = time.perf_counter()

    def callback(indata, frames_count, time_info, status):  # noqa: ANN001
        if status:
            logger.debug('[mic] %s', status)
        if stop['value']:
            raise sd.CallbackStop()
        chunks.append(float32_to_int16(np.asarray(indata, dtype=np.float32).copy()))

    logger.info('[mic] listening at %dHz', settings.input_sample_rate)
    with sd.InputStream(
        samplerate=settings.input_sample_rate,
        channels=settings.channels,
        dtype='float32',
        blocksize=settings.frames_per_chunk,
        callback=callback,
    ):
        input('Presiona Enter para detener la grabacion...')
        stop['value'] = True

    elapsed = time.perf_counter() - started
    if not chunks:
        logger.warning('[mic] no audio captured')
        return b''

    audio = np.concatenate(chunks, axis=0)
    audio = ensure_mono(audio)
    if settings.input_sample_rate != 16000:
        audio = resample_audio(audio, settings.input_sample_rate, 16000)

    duration_seconds = len(audio) / 16000.0
    rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32) / np.iinfo(np.int16).max))))
    if duration_seconds < settings.min_record_seconds:
        logger.warning(
            '[mic] ignored too short capture duration=%.2fs threshold=%.2fs',
            duration_seconds,
            settings.min_record_seconds,
        )
        return b''
    if rms < settings.silence_rms_threshold:
        logger.warning(
            '[mic] ignored silence duration=%.2fs rms=%.4f threshold=%.4f',
            duration_seconds,
            rms,
            settings.silence_rms_threshold,
        )
        return b''

    pcm = np.asarray(audio, dtype=np.int16).tobytes()
    logger.info(
        '[mic] captured duration=%.2fs elapsed=%.2fs bytes=%d rms=%.4f',
        duration_seconds,
        elapsed,
        len(pcm),
        rms,
    )
    return pcm


def play_pcm(pcm: bytes, sample_rate: int) -> None:
    audio = np.frombuffer(pcm, dtype=np.int16)
    float_audio = int16_to_float32(audio)
    logger.info('[playback] start bytes=%d rate=%dHz', len(pcm), sample_rate)
    sd.play(float_audio, samplerate=sample_rate)
    sd.wait()
    logger.info('[playback] done')


class StreamingAudioPlayer:
    def __init__(self, sample_rate: int, channels: int, enabled: bool = True, wav_path: Path | None = None) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.enabled = enabled
        self.wav_path = wav_path
        self._wav_file: wave.Wave_write | None = None
        if self.wav_path is not None:
            self.wav_path.parent.mkdir(parents=True, exist_ok=True)
            self._wav_file = wave.open(str(self.wav_path), 'wb')
            self._wav_file.setnchannels(self.channels)
            self._wav_file.setsampwidth(2)
            self._wav_file.setframerate(self.sample_rate)
            logger.info('[recording] output_wav=%s', self.wav_path)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self.max_queue_size = 0
        self.interrupted_dropped_chunks = 0
        self.received_audio_ms = 0.0
        self.written_audio_ms = 0.0
        self.last_chunk_ms = 0.0
        self.write_max_ms = 0.0
        self.stream_sample_rate: float | None = None
        self.started_at = time.perf_counter()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='gemini-audio-player', daemon=True) if self.enabled else None
        if self._thread is not None:
            self._thread.start()
        else:
            logger.info('[playback] disabled; receiving audio without local output')

    def write(self, pcm_bytes: bytes) -> None:
        if pcm_bytes and not self._stop.is_set():
            chunk_ms = len(pcm_bytes) / 2 / float(self.sample_rate) * 1000
            self.last_chunk_ms = chunk_ms
            self.received_audio_ms += chunk_ms
            if self._wav_file is not None:
                self._wav_file.writeframes(pcm_bytes)
            if not self.enabled:
                return
            self._queue.put(pcm_bytes)
            self.max_queue_size = max(self.max_queue_size, self._queue.qsize())

    def interrupt(self) -> None:
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        self.interrupted_dropped_chunks += dropped
        logger.info('[playback] interrupted dropped_chunks=%d total_interrupted_dropped_chunks=%d', dropped, self.interrupted_dropped_chunks)

    def close(self) -> None:
        self._stop.set()
        with suppress(Exception):
            self._queue.put_nowait(None)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._wav_file is not None:
            self._wav_file.close()
            self._wav_file = None

    def _run(self) -> None:
        try:
            with sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype='int16',
                blocksize=0,
            ) as stream:
                self.stream_sample_rate = float(stream.samplerate)
                logger.info('[playback] stream opened requested_rate=%d actual_rate=%.0f channels=%d', self.sample_rate, self.stream_sample_rate, self.channels)
                while not self._stop.is_set():
                    chunk = self._queue.get()
                    if chunk is None:
                        break
                    chunk_ms = len(chunk) / 2 / float(self.sample_rate) * 1000
                    write_started = time.perf_counter()
                    stream.write(chunk)
                    write_ms = (time.perf_counter() - write_started) * 1000
                    self.written_audio_ms += chunk_ms
                    self.write_max_ms = max(self.write_max_ms, write_ms)
        except Exception as exc:
            logger.warning('[playback] stream failed: %s', exc)
