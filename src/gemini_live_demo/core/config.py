"""Configuración de la demo.

``Settings`` es un dataclass inmutable con **solo valores** (defaults puros,
sin I/O): así se construye libremente en tests. La lectura del entorno vive
en ``Settings.from_env()``, el único punto que toca ``os.environ`` y ``.env``.
Esta separación evita el bug clásico de leer el entorno en tiempo de import y
hace la config trivialmente testeable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    api_key: str | None = None
    model: str = 'gemini-2.5-flash-native-audio-latest'
    language: str = 'es-ES'
    voice_name: str = 'Aoede'
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    channels: int = 1
    chunk_ms: int = 20
    continuous_mode: bool = True
    continuous_vad_silence_ms: int = 700
    continuous_vad_prefix_ms: int = 200
    audio_queue_max_chunks: int = 200
    audio_queue_log_every_chunks: int = 250
    audio_queue_warn_chunks: int = 25
    metrics_dir: str = 'metrics'
    metrics_csv_enabled: bool = True
    no_playback: bool = False
    record_output_wav: bool = False
    output_wav_dir: str = 'recordings'
    min_record_seconds: float = 0.5
    silence_rms_threshold: float = 0.01
    log_level: str = 'INFO'
    context_compression_trigger_tokens: int = 16000
    context_compression_target_tokens: int = 8000
    prompt: str = (
        'Eres un asistente conversacional en espanol. Responde breve, claro y natural. '
        'No inventes informacion. Si algo no esta claro, haz una sola pregunta. '
        'Prioriza rapidez y coherencia.'
    )
    greet_first: bool = False
    greeting_trigger: str = '[Inicio de sesion: saluda brevemente y pregunta en que puedes ayudar]'
    # Transcripcion de audio (Live API): texto de lo que dice el usuario
    # (input) y de lo que responde la IA (output). El input no cuesta tokens
    # extra. Se envia a los consumidores (web/bridge) para registro/monitoreo.
    transcribe: bool = True

    @property
    def frames_per_chunk(self) -> int:
        return max(1, int(self.input_sample_rate * self.chunk_ms / 1000))

    @classmethod
    def from_env(cls) -> 'Settings':
        """Construye la config leyendo el entorno (y ``.env`` si existe)."""
        from dotenv import load_dotenv

        load_dotenv()
        d = cls()  # defaults canónicos, definidos una sola vez arriba
        return cls(
            api_key=os.getenv('GEMINI_API_KEY'),
            model=os.getenv('GEMINI_MODEL', d.model),
            language=os.getenv('GEMINI_LANGUAGE', d.language),
            voice_name=os.getenv('GEMINI_VOICE_NAME', d.voice_name),
            input_sample_rate=int(os.getenv('GEMINI_INPUT_SAMPLE_RATE', str(d.input_sample_rate))),
            output_sample_rate=int(os.getenv('GEMINI_OUTPUT_SAMPLE_RATE', str(d.output_sample_rate))),
            channels=int(os.getenv('GEMINI_CHANNELS', str(d.channels))),
            chunk_ms=int(os.getenv('GEMINI_CHUNK_MS', str(d.chunk_ms))),
            continuous_mode=_as_bool(os.getenv('GEMINI_CONTINUOUS_MODE'), d.continuous_mode),
            continuous_vad_silence_ms=int(os.getenv('GEMINI_VAD_SILENCE_MS', str(d.continuous_vad_silence_ms))),
            continuous_vad_prefix_ms=int(os.getenv('GEMINI_VAD_PREFIX_MS', str(d.continuous_vad_prefix_ms))),
            audio_queue_max_chunks=int(os.getenv('GEMINI_AUDIO_QUEUE_MAX_CHUNKS', str(d.audio_queue_max_chunks))),
            audio_queue_log_every_chunks=int(os.getenv('GEMINI_AUDIO_QUEUE_LOG_EVERY_CHUNKS', str(d.audio_queue_log_every_chunks))),
            audio_queue_warn_chunks=int(os.getenv('GEMINI_AUDIO_QUEUE_WARN_CHUNKS', str(d.audio_queue_warn_chunks))),
            metrics_dir=os.getenv('GEMINI_METRICS_DIR', d.metrics_dir),
            metrics_csv_enabled=_as_bool(os.getenv('GEMINI_METRICS_CSV'), d.metrics_csv_enabled),
            no_playback=_as_bool(os.getenv('GEMINI_NO_PLAYBACK'), d.no_playback),
            record_output_wav=_as_bool(os.getenv('GEMINI_RECORD_OUTPUT_WAV'), d.record_output_wav),
            output_wav_dir=os.getenv('GEMINI_OUTPUT_WAV_DIR', d.output_wav_dir),
            min_record_seconds=float(os.getenv('GEMINI_MIN_RECORD_SECONDS', str(d.min_record_seconds))),
            silence_rms_threshold=float(os.getenv('GEMINI_SILENCE_RMS_THRESHOLD', str(d.silence_rms_threshold))),
            log_level=os.getenv('GEMINI_LOG_LEVEL', d.log_level),
            context_compression_trigger_tokens=int(os.getenv('GEMINI_CONTEXT_TRIGGER_TOKENS', str(d.context_compression_trigger_tokens))),
            context_compression_target_tokens=int(os.getenv('GEMINI_CONTEXT_TARGET_TOKENS', str(d.context_compression_target_tokens))),
            prompt=os.getenv('GEMINI_SYSTEM_PROMPT', d.prompt),
            greet_first=_as_bool(os.getenv('GEMINI_GREET_FIRST'), d.greet_first),
            greeting_trigger=os.getenv('GEMINI_GREETING_TRIGGER', d.greeting_trigger),
            transcribe=_as_bool(os.getenv('GEMINI_TRANSCRIBE'), d.transcribe),
        )
