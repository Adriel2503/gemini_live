from __future__ import annotations

import asyncio
import logging
import os
import queue
from pathlib import Path
import threading
import time
import wave
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

import numpy as np
import sounddevice as sd
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table

from gemini_live_demo.metrics import MetricsCsv, build_metrics_row

load_dotenv()

console = Console()
logger = logging.getLogger('gemini_live_demo')
app = typer.Typer(add_completion=False, help='Demo de voz para Gemini Live.')


def _get_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is not None and hasattr(obj, name):
            return getattr(obj, name)
    return None


@dataclass(frozen=True)
class Settings:
    api_key: str | None = os.getenv('GEMINI_API_KEY')
    model: str = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-native-audio-latest')
    language: str = os.getenv('GEMINI_LANGUAGE', 'es-ES')
    voice_name: str = os.getenv('GEMINI_VOICE_NAME', 'Aoede')
    input_sample_rate: int = int(os.getenv('GEMINI_INPUT_SAMPLE_RATE', '16000'))
    output_sample_rate: int = int(os.getenv('GEMINI_OUTPUT_SAMPLE_RATE', '24000'))
    channels: int = int(os.getenv('GEMINI_CHANNELS', '1'))
    chunk_ms: int = int(os.getenv('GEMINI_CHUNK_MS', '20'))
    continuous_mode: bool = os.getenv('GEMINI_CONTINUOUS_MODE', 'true').lower() in {'1', 'true', 'yes', 'on'}
    continuous_vad_silence_ms: int = int(os.getenv('GEMINI_VAD_SILENCE_MS', '700'))
    continuous_vad_prefix_ms: int = int(os.getenv('GEMINI_VAD_PREFIX_MS', '200'))
    audio_queue_max_chunks: int = int(os.getenv('GEMINI_AUDIO_QUEUE_MAX_CHUNKS', '200'))
    audio_queue_log_every_chunks: int = int(os.getenv('GEMINI_AUDIO_QUEUE_LOG_EVERY_CHUNKS', '250'))
    audio_queue_warn_chunks: int = int(os.getenv('GEMINI_AUDIO_QUEUE_WARN_CHUNKS', '25'))
    metrics_dir: str = os.getenv('GEMINI_METRICS_DIR', 'metrics')
    metrics_csv_enabled: bool = os.getenv('GEMINI_METRICS_CSV', 'true').lower() in {'1', 'true', 'yes', 'on'}
    no_playback: bool = os.getenv('GEMINI_NO_PLAYBACK', 'false').lower() in {'1', 'true', 'yes', 'on'}
    record_output_wav: bool = os.getenv('GEMINI_RECORD_OUTPUT_WAV', 'false').lower() in {'1', 'true', 'yes', 'on'}
    output_wav_dir: str = os.getenv('GEMINI_OUTPUT_WAV_DIR', 'recordings')
    min_record_seconds: float = float(os.getenv('GEMINI_MIN_RECORD_SECONDS', '0.5'))
    silence_rms_threshold: float = float(os.getenv('GEMINI_SILENCE_RMS_THRESHOLD', '0.01'))
    log_level: str = os.getenv('GEMINI_LOG_LEVEL', 'INFO')
    context_compression_trigger_tokens: int = int(os.getenv('GEMINI_CONTEXT_TRIGGER_TOKENS', '16000'))
    context_compression_target_tokens: int = int(os.getenv('GEMINI_CONTEXT_TARGET_TOKENS', '8000'))
    prompt: str = os.getenv(
        'GEMINI_SYSTEM_PROMPT',
        'Eres un asistente conversacional en espanol. Responde breve, claro y natural. '
        'No inventes informacion. Si algo no esta claro, haz una sola pregunta. '
        'Prioriza rapidez y coherencia.',
    )

    @property
    def frames_per_chunk(self) -> int:
        return max(1, int(self.input_sample_rate * self.chunk_ms / 1000))


@dataclass
class EventSummary:
    text: str | None
    audio_chunks: list[bytes]
    done: bool
    turn_complete: bool
    generation_complete: bool
    interrupted: bool
    go_away: bool
    model_turn_present: bool
    new_handle: str | None
    resumable: bool | None
    voice_activity_type: str | None = None
    voice_activity_offset: str | None = None
    vad_signal_type: str | None = None


class MockLiveSession:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self._last_input = ''

    async def __aenter__(self) -> 'MockLiveSession':
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def send_realtime_input(self, **kwargs: Any) -> None:
        text = kwargs.get('text')
        if text:
            self._last_input = str(text)
            return
        if kwargs.get('audio') is not None:
            self._last_input = 'audio'

    async def receive(self) -> AsyncIterator[Any]:
        text = f'Modo mock. Ultimo input: {self._last_input or "vacio"}'
        yield {'server_content': {'output_transcription': {'text': text}, 'generation_complete': True}}


class GeminiLiveAdapter:
    def __init__(self, api_key: str, model: str, prompt: str, settings: Settings, mock: bool = False) -> None:
        self.api_key = api_key
        self.model = model
        self.prompt = prompt
        self.settings = settings
        self.mock = mock
        self._client = None
        self._types = None
        self._session_handle: str | None = None
        self._refresh_requested = False

    @property
    def session_handle(self) -> str | None:
        return self._session_handle

    @property
    def should_refresh_session(self) -> bool:
        return self._refresh_requested

    def clear_refresh_request(self) -> None:
        self._refresh_requested = False

    def _load_sdk(self) -> None:
        if self._client is not None:
            return
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=self.api_key)
        self._types = types

    def _build_config(self):
        types = self._types
        modality_audio = getattr(getattr(types, 'Modality', None), 'AUDIO', 'AUDIO')
        voice_config = types.VoiceConfig(
            prebuiltVoiceConfig=types.PrebuiltVoiceConfig(voiceName=self.settings.voice_name)
        )
        speech_config = types.SpeechConfig(
            voiceConfig=voice_config,
            languageCode=normalize_language_code(self.settings.language, self.model),
        )
        compression = types.ContextWindowCompressionConfig(
            triggerTokens=self.settings.context_compression_trigger_tokens,
            slidingWindow=types.SlidingWindow(targetTokens=self.settings.context_compression_target_tokens),
        )
        if self.settings.continuous_mode:
            realtime_input_config = types.RealtimeInputConfig(
                automaticActivityDetection=types.AutomaticActivityDetection(
                    disabled=False,
                    prefixPaddingMs=self.settings.continuous_vad_prefix_ms,
                    silenceDurationMs=self.settings.continuous_vad_silence_ms,
                ),
                activityHandling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turnCoverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            )
        else:
            realtime_input_config = types.RealtimeInputConfig(
                automaticActivityDetection=types.AutomaticActivityDetection(disabled=True),
                activityHandling=types.ActivityHandling.NO_INTERRUPTION,
                turnCoverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            )
        session_resumption = types.SessionResumptionConfig(
            handle=self._session_handle,
        )
        return types.LiveConnectConfig(
            systemInstruction=self.prompt,
            responseModalities=[modality_audio],
            speechConfig=speech_config,
            contextWindowCompression=compression,
            realtimeInputConfig=realtime_input_config,
            sessionResumption=session_resumption,
        )

    async def connect(self):
        if self.mock:
            return MockLiveSession(self.prompt)

        self._load_sdk()
        live_api = getattr(getattr(self._client, 'aio', None), 'live', None)
        if live_api is None or not hasattr(live_api, 'connect'):
            raise RuntimeError('El SDK instalado no expone client.aio.live.connect().')

        config = self._build_config()
        logger.info(
            '[session] opening model=%s resumable_handle=%s',
            self.model,
            'yes' if self._session_handle else 'no',
        )
        return live_api.connect(model=self.model, config=config)

    def note_event(self, summary: EventSummary) -> None:
        if summary.new_handle and summary.new_handle != self._session_handle:
            self._session_handle = summary.new_handle
            logger.info('[session] updated resumption handle resumable=%s', summary.resumable)
        if summary.go_away:
            self._refresh_requested = True
            logger.warning('[session] go_away received; will reconnect after current turn')

    async def send_text(self, session: Any, text: str) -> None:
        if hasattr(session, 'send_realtime_input'):
            await session.send_realtime_input(text=text)
            return
        await session.send(input=text, end_of_turn=True)

    async def send_audio(self, session: Any, pcm_bytes: bytes) -> None:
        duration_seconds, rms, byte_count = summarize_pcm(pcm_bytes, self.settings.input_sample_rate)
        logger.info(
            '[send] audio turn duration=%.2fs bytes=%d rms=%.4f rate=%dHz',
            duration_seconds,
            byte_count,
            rms,
            self.settings.input_sample_rate,
        )
        types = self._types
        chunk_size = self.settings.frames_per_chunk * 2
        chunks_sent = 0
        chunk_delay = self.settings.frames_per_chunk / float(self.settings.input_sample_rate)
        await session.send_realtime_input(activity_start=types.ActivityStart())
        for chunk in chunk_pcm_bytes(pcm_bytes, chunk_size):
            blob = types.Blob(data=chunk, mimeType=f'audio/pcm;rate={self.settings.input_sample_rate}')
            await session.send_realtime_input(audio=blob)
            chunks_sent += 1
            await asyncio.sleep(chunk_delay)
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        logger.info('[send] activity ended chunks=%d chunk_ms=%d', chunks_sent, self.settings.chunk_ms)

    async def send_audio_chunk(self, session: Any, pcm_bytes: bytes) -> None:
        types = self._types
        blob = types.Blob(data=pcm_bytes, mimeType=f'audio/pcm;rate={self.settings.input_sample_rate}')
        await session.send_realtime_input(audio=blob)

    async def receive(self, session: Any) -> AsyncIterator[Any]:
        async for event in session.receive():
            yield event


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

def configure_logging() -> None:
    level_name = os.getenv('GEMINI_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(message)s',
        datefmt='[%X]',
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
        force=True,
    )
    logger.setLevel(level)


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


def float32_to_int16(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * np.iinfo(np.int16).max).astype(np.int16)


def int16_to_float32(audio: np.ndarray) -> np.ndarray:
    return np.asarray(audio, dtype=np.int16).astype(np.float32) / np.iinfo(np.int16).max


def ensure_mono(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio)
    if audio.ndim == 1:
        return audio
    return np.mean(audio, axis=1)


def normalize_language_code(language: str, model: str) -> str:
    language = language.strip()
    if 'native-audio' in model and language in {'es', 'es-ES', 'es-419'}:
        return 'es-US'

    aliases = {
        'es': 'es-ES',
        'en': 'en-US',
        'pt': 'pt-BR',
    }
    return aliases.get(language, language)

def summarize_pcm(pcm: bytes, sample_rate: int) -> tuple[float, float, int]:
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0, 0.0, 0
    duration_seconds = samples.size / float(sample_rate)
    norm = samples.astype(np.float32) / np.iinfo(np.int16).max
    rms = float(np.sqrt(np.mean(np.square(norm))))
    return duration_seconds, rms, len(pcm)


def chunk_pcm_bytes(pcm: bytes, chunk_size: int) -> list[bytes]:
    return [pcm[i:i + chunk_size] for i in range(0, len(pcm), chunk_size) if pcm[i:i + chunk_size]]


def resample_audio(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    from math import gcd
    from scipy.signal import resample_poly

    factor = gcd(source_rate, target_rate)
    up = target_rate // factor
    down = source_rate // factor
    return resample_poly(audio, up, down).astype(audio.dtype, copy=False)


def capture_until_enter(settings: Settings) -> bytes:
    chunks: list[np.ndarray] = []
    stop = {'value': False}
    started = time.perf_counter()

    def callback(indata, frames_count, time_info, status):  # noqa: ANN001
        if status:
            console.print(f'[yellow]{status}[/yellow]')
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


def summarize_event(event: Any) -> EventSummary:
    text: str | None = None
    audio_chunks: list[bytes] = []
    turn_complete = False
    generation_complete = False
    interrupted = False
    go_away = False
    model_turn_present = False
    new_handle: str | None = None
    resumable: bool | None = None
    voice_activity_type: str | None = None
    voice_activity_offset: str | None = None
    vad_signal_type: str | None = None

    if isinstance(event, dict):
        sc = event.get('server_content') or {}
        out = sc.get('output_transcription') or {}
        text = out.get('text') or None
        turn_complete = bool(sc.get('turn_complete'))
        generation_complete = bool(sc.get('generation_complete'))
        interrupted = bool(sc.get('interrupted'))
        go_away = bool(event.get('go_away'))
        session_update = event.get('session_resumption_update') or {}
        new_handle = session_update.get('new_handle') or session_update.get('newHandle')
        resumable = session_update.get('resumable')
        voice_activity = event.get('voice_activity') or event.get('voiceActivity') or {}
        voice_activity_type = voice_activity.get('voice_activity_type') or voice_activity.get('voiceActivityType')
        voice_activity_offset = voice_activity.get('audio_offset') or voice_activity.get('audioOffset')
        vad_signal = event.get('voice_activity_detection_signal') or event.get('voiceActivityDetectionSignal') or {}
        vad_signal_type = vad_signal.get('vad_signal_type') or vad_signal.get('vadSignalType')
        model_turn = sc.get('model_turn')
        model_turn_present = bool(model_turn)
        return EventSummary(
            text=text,
            audio_chunks=audio_chunks,
            done=turn_complete or generation_complete,
            turn_complete=turn_complete,
            generation_complete=generation_complete,
            interrupted=interrupted,
            go_away=go_away,
            model_turn_present=model_turn_present,
            new_handle=new_handle,
            resumable=resumable,
            voice_activity_type=str(voice_activity_type) if voice_activity_type else None,
            voice_activity_offset=str(voice_activity_offset) if voice_activity_offset else None,
            vad_signal_type=str(vad_signal_type) if vad_signal_type else None,
        )

    sc = getattr(event, 'server_content', None)
    if sc is not None:
        out = _get_attr(sc, 'output_transcription', 'outputTranscription')
        if out is not None:
            text = _get_attr(out, 'text') or text
        mt = _get_attr(sc, 'model_turn', 'modelTurn')
        if mt is not None:
            model_turn_present = True
            for part in getattr(mt, 'parts', []) or []:
                inline = _get_attr(part, 'inline_data', 'inlineData')
                if inline is not None:
                    data = _get_attr(inline, 'data')
                    if data:
                        audio_chunks.append(bytes(data))
        turn_complete = bool(_get_attr(sc, 'turn_complete', 'turnComplete'))
        generation_complete = bool(_get_attr(sc, 'generation_complete', 'generationComplete'))
        interrupted = bool(_get_attr(sc, 'interrupted'))

    voice_activity = _get_attr(event, 'voice_activity', 'voiceActivity')
    if voice_activity is not None:
        voice_activity_type = _get_attr(voice_activity, 'voice_activity_type', 'voiceActivityType')
        voice_activity_offset = _get_attr(voice_activity, 'audio_offset', 'audioOffset')
    vad_signal = _get_attr(event, 'voice_activity_detection_signal', 'voiceActivityDetectionSignal')
    if vad_signal is not None:
        vad_signal_type = _get_attr(vad_signal, 'vad_signal_type', 'vadSignalType')

    go_away = bool(_get_attr(event, 'go_away', 'goAway'))
    session_update = _get_attr(event, 'session_resumption_update', 'sessionResumptionUpdate')
    if session_update is not None:
        new_handle = _get_attr(session_update, 'new_handle', 'newHandle')
        resumable = _get_attr(session_update, 'resumable')

    return EventSummary(
        text=text,
        audio_chunks=audio_chunks,
        done=turn_complete or generation_complete,
        turn_complete=turn_complete,
        generation_complete=generation_complete,
        interrupted=interrupted,
        go_away=go_away,
        model_turn_present=model_turn_present,
        new_handle=new_handle,
        resumable=resumable,
    )


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
        dropped_chunks = {'count': 0}
        queue_stats = {'max_size': 0}
        stream_stats = {
            'send_chunks': 0,
            'send_bytes': 0,
            'send_total_ms': 0.0,
            'send_max_ms': 0.0,
            'send_over_budget_count': 0,
            'send_while_model_speaking_chunks': 0,
            'model_speaking': False,
        }
        vad_stats = {'start_count': 0, 'end_count': 0, 'last_type': None, 'last_offset': None}
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
                dropped_chunks['count'] += 1

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
                    stream_stats['send_chunks'] += 1
                    stream_stats['send_bytes'] += len(chunk)
                    stream_stats['send_total_ms'] += send_ms
                    stream_stats['send_max_ms'] = max(stream_stats['send_max_ms'], send_ms)
                    if send_ms > self.settings.chunk_ms:
                        stream_stats['send_over_budget_count'] += 1
                    if stream_stats['model_speaking']:
                        stream_stats['send_while_model_speaking_chunks'] += 1
                    queue_size = audio_queue.qsize()
                    queue_stats['max_size'] = max(queue_stats['max_size'], queue_size)
                    queue_delay_ms = queue_size * self.settings.chunk_ms
                    if queue_size >= self.settings.audio_queue_warn_chunks:
                        logger.warning(
                            '[mic] queue backlog queue_size=%d queue_delay_ms=%d dropped_chunks=%d',
                            queue_size,
                            queue_delay_ms,
                            dropped_chunks['count'],
                        )
                    if sent_chunks % self.settings.audio_queue_log_every_chunks == 0:
                        logger.info(
                            '[mic] streaming chunks_sent=%d queue_size=%d queue_delay_ms=%d dropped_chunks=%d send_avg_ms=%.2f send_max_ms=%.2f send_over_budget=%d model_speaking=%s',
                            sent_chunks,
                            queue_size,
                            queue_delay_ms,
                            dropped_chunks['count'],
                            stream_stats['send_total_ms'] / max(1, stream_stats['send_chunks']),
                            stream_stats['send_max_ms'],
                            stream_stats['send_over_budget_count'],
                            'yes' if stream_stats['model_speaking'] else 'no',
                        )
            logger.info('[mic] continuous stream closed chunks_sent=%d dropped_chunks=%d', sent_chunks, dropped_chunks['count'])

        async def receive_model() -> None:
            turn_index = 0
            event_count = 0
            response_audio_chunks = 0
            response_audio_bytes = 0
            text_parts: list[str] = []
            turn_started_at: float | None = None
            first_audio_at: float | None = None
            interrupted_seen = False
            while not stop_event.is_set():
                assert self._session is not None
                saw_event = False
                async for event in self.adapter.receive(self._session):
                    saw_event = True
                    if stop_event.is_set():
                        break
                    now = time.perf_counter()
                    event_count += 1
                    if turn_started_at is None:
                        turn_started_at = now
                    summary = summarize_event(event)
                    self.adapter.note_event(summary)
                    if summary.voice_activity_type or summary.vad_signal_type:
                        vad_type = summary.voice_activity_type or summary.vad_signal_type
                        vad_stats['last_type'] = vad_type
                        vad_stats['last_offset'] = summary.voice_activity_offset
                        if vad_type and ('START' in vad_type or 'SOS' in vad_type):
                            vad_stats['start_count'] += 1
                        if vad_type and ('END' in vad_type or 'EOS' in vad_type):
                            vad_stats['end_count'] += 1
                        logger.info('[vad] type=%s offset=%s', vad_type, summary.voice_activity_offset)
                    if summary.interrupted:
                        interrupted_seen = True
                        stream_stats['model_speaking'] = False
                        logger.warning('[live] interrupted=true')
                        player.interrupt()
                    if summary.text:
                        text_parts.append(summary.text)
                    if summary.audio_chunks:
                        stream_stats['model_speaking'] = True
                        if first_audio_at is None:
                            first_audio_at = now
                        for chunk in summary.audio_chunks:
                            response_audio_chunks += 1
                            response_audio_bytes += len(chunk)
                            player.write(chunk)
                    if summary.generation_complete or summary.turn_complete:
                        response_text = ''.join(text_parts).strip()
                        has_turn_payload = bool(response_text or response_audio_chunks or interrupted_seen)
                        if not has_turn_payload:
                            logger.info('[live] ignored empty turn marker generation_complete=%s turn_complete=%s', summary.generation_complete, summary.turn_complete)
                            event_count = 0
                            turn_started_at = None
                            first_audio_at = None
                            continue
                        turn_index += 1
                        elapsed_ms = int((now - turn_started_at) * 1000) if turn_started_at is not None else 0
                        first_audio_latency_ms = (
                            int((first_audio_at - turn_started_at) * 1000)
                            if first_audio_at is not None and turn_started_at is not None
                            else None
                        )
                        max_queue_size = queue_stats['max_size']
                        max_queue_delay_ms = max_queue_size * self.settings.chunk_ms
                        logger.info(
                            '[live] turn done events=%d text=%s audio_chunks=%d audio_bytes=%d first_audio_latency_ms=%s duration_ms=%d max_queue_size=%d dropped_chunks=%d generation_complete=%s turn_complete=%s',
                            event_count,
                            'yes' if response_text else 'no',
                            response_audio_chunks,
                            response_audio_bytes,
                            first_audio_latency_ms if first_audio_latency_ms is not None else 'n/a',
                            elapsed_ms,
                            max_queue_size,
                            dropped_chunks['count'],
                            'yes' if summary.generation_complete else 'no',
                            'yes' if summary.turn_complete else 'no',
                        )
                        metrics.write_row(
                            build_metrics_row(
                                settings=self.settings,
                                session_id=session_id,
                                turn_index=turn_index,
                                language=normalize_language_code(self.settings.language, self.settings.model),
                                event_count=event_count,
                                response_text=response_text,
                                response_audio_chunks=response_audio_chunks,
                                response_audio_bytes=response_audio_bytes,
                                generation_complete=summary.generation_complete,
                                turn_complete=summary.turn_complete,
                                interrupted=interrupted_seen,
                                first_audio_latency_ms=first_audio_latency_ms,
                                turn_duration_ms=elapsed_ms,
                                max_queue_size=max_queue_size,
                                dropped_chunks=dropped_chunks['count'],
                                stream_stats=stream_stats,
                                vad_stats=vad_stats,
                                player=player,
                                player_elapsed_ms=(time.perf_counter() - player.started_at) * 1000,
                                created_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                            )
                        )
                        stream_stats['model_speaking'] = False
                        if response_text:
                            logger.info('[live] gemini_text=%s', response_text)
                        event_count = 0
                        response_audio_chunks = 0
                        response_audio_bytes = 0
                        text_parts.clear()
                        turn_started_at = None
                        first_audio_at = None
                        interrupted_seen = False
                        queue_stats['max_size'] = audio_queue.qsize()
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
            last_summary: EventSummary | None = None
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
                    last_summary = summary
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
            except Exception as exc:
                logger.warning('[turn %d] receive failed: %s', turn_index, exc)
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


@app.command('list-devices')
def list_devices_cmd() -> None:
    configure_logging()
    list_devices()


@app.command('test-mic')
def test_mic_cmd(seconds: int = typer.Option(5, min=1, help='Duracion en segundos.')) -> None:
    configure_logging()
    settings = Settings()
    frames: list[np.ndarray] = []
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, frames_count, time_info, status):  # noqa: ANN001
        if status:
            console.print(f'[yellow]{status}[/yellow]')
        q.put(indata.copy())

    console.print(f'Grabando {seconds}s desde el microfono...')
    with sd.InputStream(
        samplerate=settings.input_sample_rate,
        channels=settings.channels,
        dtype='float32',
        callback=callback,
    ):
        start = time.perf_counter()
        with Progress() as progress:
            task = progress.add_task('capturando', total=seconds)
            while time.perf_counter() - start < seconds:
                try:
                    frames.append(q.get(timeout=0.1))
                except queue.Empty:
                    pass
                progress.update(task, completed=time.perf_counter() - start)

    if not frames:
        console.print('[red]No se capturo audio.[/red]')
        return

    audio = np.concatenate(frames, axis=0)
    rms = float(np.sqrt(np.mean(np.square(audio))))
    console.print(f'[green]Audio capturado[/green] frames={len(audio)} rms={rms:.4f}')


@app.command()
def run(
    mock: bool = typer.Option(False, help='Usa una sesion mock local.'),
    manual: bool = typer.Option(False, help='Usa Enter para grabar/enviar cada turno en vez de streaming continuo.'),
    no_playback: bool = typer.Option(False, help='Diagnostico: recibe audio de Gemini pero no lo reproduce localmente.'),
    record_output_wav: bool = typer.Option(False, help='Diagnostico: guarda el audio de Gemini en WAV sin reproducirlo.'),
) -> None:
    configure_logging()
    settings = Settings()
    if manual:
        settings = replace(settings, continuous_mode=False)
    if no_playback:
        settings = replace(settings, no_playback=True)
    if record_output_wav:
        settings = replace(settings, record_output_wav=True, no_playback=True)
    if not mock and not settings.api_key:
        raise typer.BadParameter('Falta GEMINI_API_KEY en el entorno o en .env')

    adapter = GeminiLiveAdapter(
        api_key=settings.api_key or 'mock',
        model=settings.model,
        prompt=settings.prompt,
        settings=settings,
        mock=mock,
    )
    asyncio.run(DemoRunner(adapter, settings).run())


if __name__ == '__main__':
    app()




































