"""Tests del parsing de eventos (``gemini_live_demo.core.events``).

Cubre las dos rutas de ``summarize_event``: el dict crudo del transporte
y el objeto tipado del SDK. Determinista, sin red. Ejecutable con pytest o
directamente (``python tests/test_events.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

from gemini_live_demo.core.events import summarize_event


# --- Ruta dict --------------------------------------------------------------

def test_dict_extracts_text_and_generation_complete():
    event = {
        'server_content': {
            'output_transcription': {'text': 'hola'},
            'generation_complete': True,
        }
    }
    s = summarize_event(event)
    assert s.text == 'hola'
    assert s.generation_complete is True
    assert s.done is True  # done = turn_complete or generation_complete


def test_dict_turn_complete_marks_done():
    s = summarize_event({'server_content': {'turn_complete': True}})
    assert s.turn_complete is True
    assert s.done is True


def test_dict_go_away_and_session_handle():
    event = {
        'go_away': True,
        'session_resumption_update': {'new_handle': 'h-123', 'resumable': True},
    }
    s = summarize_event(event)
    assert s.go_away is True
    assert s.new_handle == 'h-123'
    assert s.resumable is True


def test_dict_voice_activity_camel_and_snake():
    s = summarize_event({'voiceActivity': {'voiceActivityType': 'START', 'audioOffset': '42'}})
    assert s.voice_activity_type == 'START'
    assert s.voice_activity_offset == '42'


def test_dict_empty_event_is_all_false():
    s = summarize_event({})
    assert s.text is None
    assert s.done is False
    assert s.audio_chunks == []


# --- Ruta objeto (SDK tipado) ----------------------------------------------

def test_object_extracts_inline_audio_chunks():
    part = SimpleNamespace(inline_data=SimpleNamespace(data=b'\x01\x02'))
    model_turn = SimpleNamespace(parts=[part])
    sc = SimpleNamespace(
        model_turn=model_turn,
        output_transcription=SimpleNamespace(text='texto'),
        turn_complete=False,
        generation_complete=True,
        interrupted=False,
    )
    event = SimpleNamespace(server_content=sc)
    s = summarize_event(event)
    assert s.model_turn_present is True
    assert s.audio_chunks == [b'\x01\x02']
    assert s.text == 'texto'
    assert s.generation_complete is True
    assert s.done is True


def test_object_interrupted_flag():
    sc = SimpleNamespace(
        model_turn=None,
        output_transcription=None,
        turn_complete=False,
        generation_complete=False,
        interrupted=True,
    )
    s = summarize_event(SimpleNamespace(server_content=sc))
    assert s.interrupted is True
    assert s.done is False


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
