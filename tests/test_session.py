"""Tests de la lógica pura de ``GeminiLiveAdapter.note_event``.

No tocan red ni SDK: ``note_event`` solo actualiza estado interno a partir de
un ``EventSummary`` (handle de reanudación y bandera de refresh por go_away).
"""

from __future__ import annotations

from gemini_live_demo.core.config import Settings
from gemini_live_demo.core.events import EventSummary
from gemini_live_demo.core.session import GeminiLiveAdapter


def _adapter() -> GeminiLiveAdapter:
    return GeminiLiveAdapter(api_key='k', model='m', prompt='p', settings=Settings(), mock=True)


def _summary(**overrides) -> EventSummary:
    base = dict(
        text=None,
        audio_chunks=[],
        done=False,
        turn_complete=False,
        generation_complete=False,
        interrupted=False,
        go_away=False,
        model_turn_present=False,
        new_handle=None,
        resumable=None,
    )
    base.update(overrides)
    return EventSummary(**base)


def test_note_event_updates_resumption_handle():
    adapter = _adapter()
    assert adapter.session_handle is None
    adapter.note_event(_summary(new_handle='handle-1', resumable=True))
    assert adapter.session_handle == 'handle-1'
    # Un handle nuevo lo reemplaza.
    adapter.note_event(_summary(new_handle='handle-2'))
    assert adapter.session_handle == 'handle-2'


def test_note_event_ignores_empty_or_same_handle():
    adapter = _adapter()
    adapter.note_event(_summary(new_handle='h'))
    adapter.note_event(_summary(new_handle=None))  # sin handle: no cambia
    assert adapter.session_handle == 'h'
    adapter.note_event(_summary(new_handle='h'))  # mismo handle: idempotente
    assert adapter.session_handle == 'h'


def test_go_away_requests_refresh_and_can_be_cleared():
    adapter = _adapter()
    assert adapter.should_refresh_session is False
    adapter.note_event(_summary(go_away=True))
    assert adapter.should_refresh_session is True
    adapter.clear_refresh_request()
    assert adapter.should_refresh_session is False


def test_normal_event_does_not_request_refresh():
    adapter = _adapter()
    adapter.note_event(_summary(text='hola', new_handle='h'))
    assert adapter.should_refresh_session is False


def _main() -> int:
    tests = [
        test_note_event_updates_resumption_handle,
        test_note_event_ignores_empty_or_same_handle,
        test_go_away_requests_refresh_and_can_be_cleared,
        test_normal_event_does_not_request_refresh,
    ]
    for test in tests:
        test()
    print(f'\n{len(tests)}/{len(tests)} passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
