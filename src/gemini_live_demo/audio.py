"""Utilidades de audio puras (sin hardware ni I/O).

Todo lo de este modulo es determinista y unit-testeable: conversiones de
formato PCM, resample, mezcla a mono, y normalizacion del codigo de idioma.
"""

from __future__ import annotations

import numpy as np


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
