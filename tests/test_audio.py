"""Tests de las utilidades de audio puras (``gemini_live_demo.audio``).

Determinista, sin hardware. Ejecutable con pytest o directamente
(``python tests/test_audio.py``).
"""

from __future__ import annotations

import numpy as np

from gemini_live_demo.core.audio import (
    chunk_pcm_bytes,
    ensure_mono,
    float32_to_int16,
    int16_to_float32,
    normalize_language_code,
    summarize_pcm,
)


def test_float32_to_int16_clips_out_of_range():
    out = float32_to_int16(np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float32))
    assert out[0] == out[1]  # -2.0 se recorta a -1.0
    assert out[3] == out[4]  # 2.0 se recorta a 1.0
    assert out[2] == 0
    assert out.dtype == np.int16


def test_float_int_roundtrip_is_close():
    original = np.array([0.0, 0.25, -0.5, 0.75], dtype=np.float32)
    restored = int16_to_float32(float32_to_int16(original))
    assert np.allclose(original, restored, atol=1e-4)


def test_ensure_mono_averages_channels():
    stereo = np.array([[1.0, 3.0], [2.0, 4.0]], dtype=np.float32)
    mono = ensure_mono(stereo)
    assert mono.ndim == 1
    assert list(mono) == [2.0, 3.0]


def test_ensure_mono_passthrough_when_already_mono():
    mono = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert list(ensure_mono(mono)) == [1.0, 2.0, 3.0]


def test_normalize_language_native_audio_forces_es_us():
    for lang in ('es', 'es-ES', 'es-419'):
        assert normalize_language_code(lang, 'gemini-2.5-flash-native-audio-latest') == 'es-US'


def test_normalize_language_non_native_uses_aliases():
    assert normalize_language_code('es', 'gemini-3.1-flash-live-preview') == 'es-ES'
    assert normalize_language_code('en', 'gemini-3.1-flash-live-preview') == 'en-US'
    assert normalize_language_code('pt', 'gemini-3.1-flash-live-preview') == 'pt-BR'


def test_normalize_language_unknown_passes_through():
    assert normalize_language_code('fr-FR', 'cualquier-modelo') == 'fr-FR'


def test_summarize_pcm_empty_is_zero():
    assert summarize_pcm(b'', 16000) == (0.0, 0.0, 0)


def test_summarize_pcm_known_signal():
    samples = np.array([1000, -1000, 1000, -1000], dtype=np.int16)
    duration, rms, byte_count = summarize_pcm(samples.tobytes(), 16000)
    assert duration == 4 / 16000.0
    assert byte_count == 8
    assert rms > 0.0


def test_chunk_pcm_bytes_splits_and_drops_empty_tail():
    data = b'abcdefg'  # 7 bytes, chunk_size=3 -> abc def g
    chunks = chunk_pcm_bytes(data, 3)
    assert chunks == [b'abc', b'def', b'g']


def test_chunk_pcm_bytes_empty_input():
    assert chunk_pcm_bytes(b'', 4) == []


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
