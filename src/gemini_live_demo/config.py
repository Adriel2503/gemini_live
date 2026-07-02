from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
