"""Tests del puente WebSocket <-> Gemini (``web.server._bridge``).

No usan red ni API: inyectan un adaptador y un WebSocket falsos para
verificar de forma determinista cada direccion del puente.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gemini_live_demo.core.config import Settings
from gemini_live_demo.web.server import _bridge


class _FakeCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return None


class _FakeAdapter:
    """Adaptador falso: no habla con Gemini, solo registra/emite eventos."""

    model = 'fake'

    def __init__(self, events, block_receive=False):
        self._events = events
        self._block_receive = block_receive
        self.sent_audio: list[bytes] = []

    def clear_refresh_request(self):
        pass

    def note_event(self, summary):
        pass

    async def connect(self):
        return _FakeCM(object())

    async def send_audio_chunk(self, session, data):
        self.sent_audio.append(data)

    async def receive(self, session):
        if self._block_receive:
            while True:  # nunca emite; se cancela al cerrar el puente
                await asyncio.sleep(0.02)
                if False:  # pragma: no cover - marca esto como generador async
                    yield None
        for event in self._events:
            yield event


class _FakeWS:
    """WebSocket falso al estilo ASGI."""

    def __init__(self, incoming=None, block_receive=False):
        self._incoming = list(incoming or [])
        self._block_receive = block_receive
        self.sent_json: list[dict] = []
        self.sent_bytes: list[bytes] = []

    async def send_json(self, obj):
        self.sent_json.append(obj)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        if self._block_receive:
            await asyncio.Event().wait()  # bloquea hasta cancelacion
        return {'type': 'websocket.disconnect'}


def _audio_event(data: bytes):
    """Evento estilo-objeto del SDK con un chunk de audio inline."""
    part = SimpleNamespace(inline_data=SimpleNamespace(data=data))
    model_turn = SimpleNamespace(parts=[part])
    return SimpleNamespace(server_content=SimpleNamespace(model_turn=model_turn))


def test_bridge_gemini_to_browser():
    """Los eventos de Gemini (texto, audio, fin de turno) llegan al navegador."""
    events = [
        {'server_content': {'output_transcription': {'text': 'hola'}}},
        _audio_event(b'PCMDATA'),
        {'server_content': {'turn_complete': True}},
        {'go_away': True},  # corta el puente limpio
    ]
    adapter = _FakeAdapter(events)
    ws = _FakeWS(block_receive=True)  # el navegador no envia nada; bloquea

    asyncio.run(_bridge(ws, adapter, Settings()))

    assert any(m.get('type') == 'status' and m.get('state') == 'ready' for m in ws.sent_json), ws.sent_json
    assert any(m.get('type') == 'text' and m.get('text') == 'hola' for m in ws.sent_json)
    assert any(m.get('type') == 'turn_complete' for m in ws.sent_json)
    assert ws.sent_bytes == [b'PCMDATA']


def test_bridge_browser_to_gemini():
    """El audio del navegador se reenvia a Gemini y el disconnect cierra el puente."""
    adapter = _FakeAdapter(events=[], block_receive=True)  # Gemini calla
    ws = _FakeWS(incoming=[
        {'type': 'websocket.receive', 'bytes': b'chunk1'},
        {'type': 'websocket.receive', 'bytes': b'chunk2'},
        {'type': 'websocket.disconnect'},
    ])

    asyncio.run(_bridge(ws, adapter, Settings()))

    assert adapter.sent_audio == [b'chunk1', b'chunk2']


def _main() -> int:
    tests = [test_bridge_gemini_to_browser, test_bridge_browser_to_gemini]
    passed = 0
    for test in tests:
        test()
        passed += 1
    print(f'\n{passed}/{len(tests)} passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
