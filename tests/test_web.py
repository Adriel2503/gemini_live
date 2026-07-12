"""Tests del puente WebSocket <-> Gemini (``web.server._bridge``).

No usan red ni API: inyectan un adaptador y un WebSocket falsos para
verificar de forma determinista cada direccion del puente.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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

    async def greet(self, session):
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

    asyncio.run(_bridge(ws, adapter))

    assert any(m.get('type') == 'status' and m.get('state') == 'ready' for m in ws.sent_json), ws.sent_json
    assert any(m.get('type') == 'text' and m.get('text') == 'hola' for m in ws.sent_json)
    assert any(m.get('type') == 'turn_complete' for m in ws.sent_json)
    assert ws.sent_bytes == [b'PCMDATA']


def test_bridge_forwards_usage_metadata():
    """Los tokens del turno (usage_metadata) llegan al navegador como 'usage'."""
    events = [
        {'usage_metadata': {'prompt_token_count': 50, 'response_token_count': 20, 'total_token_count': 70}},
        {'go_away': True},
    ]
    adapter = _FakeAdapter(events)
    ws = _FakeWS(block_receive=True)

    asyncio.run(_bridge(ws, adapter))

    usage_msgs = [m for m in ws.sent_json if m.get('type') == 'usage']
    assert usage_msgs == [{
        'type': 'usage', 'prompt_tokens': 50, 'response_tokens': 20, 'cached_tokens': None, 'total_tokens': 70,
        'prompt_tokens_by_modality': None, 'response_tokens_by_modality': None,
    }]


def test_models_endpoint_lists_allowlist_and_default():
    """``/models`` expone la allowlist y un default válido."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import _ALLOWED_MODEL_IDS, app

    res = TestClient(app).get('/models')
    assert res.status_code == 200
    data = res.json()
    ids = {m['id'] for m in data['models']}
    assert ids == _ALLOWED_MODEL_IDS
    assert data['default'] in _ALLOWED_MODEL_IDS


def test_call_sin_bridge_configurado_responde_503(monkeypatch):
    """Sin BRIDGE_URL el proxy /call responde 503 y no intenta red."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    monkeypatch.delenv('BRIDGE_URL', raising=False)
    res = TestClient(app).post('/call', json={'number': '987654321'})
    assert res.status_code == 503
    assert res.json()['success'] is False


def test_call_proxyea_al_bridge(monkeypatch):
    """Con BRIDGE_URL configurado, /call reenvía número y token al bridge."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web import server

    seen = {}

    def fake_post(bridge_url, token, number, model=''):
        seen.update(url=bridge_url, token=token, number=number, model=model)
        return 200, {'success': True, 'uuid': 'abc'}

    monkeypatch.setenv('BRIDGE_URL', 'http://bridge:9094')
    monkeypatch.setenv('BRIDGE_TOKEN', 'tok123')
    monkeypatch.setattr(server, '_post_to_bridge', fake_post)

    res = TestClient(server.app).post('/call', json={'number': '987654321'})
    assert res.status_code == 200
    assert res.json() == {'success': True, 'uuid': 'abc'}
    assert seen == {'url': 'http://bridge:9094', 'token': 'tok123', 'number': '987654321', 'model': ''}


def test_call_sin_numero_responde_400(monkeypatch):
    """Numero vacío se rechaza en el proxy sin llegar al bridge."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    monkeypatch.setenv('BRIDGE_URL', 'http://bridge:9094')
    res = TestClient(app).post('/call', json={})
    assert res.status_code == 400


def test_models_expone_call_enabled(monkeypatch):
    """``call_enabled`` refleja si BRIDGE_URL está configurado."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    monkeypatch.delenv('BRIDGE_URL', raising=False)
    assert TestClient(app).get('/models').json()['call_enabled'] is False
    monkeypatch.setenv('BRIDGE_URL', 'http://bridge:9094')
    assert TestClient(app).get('/models').json()['call_enabled'] is True


def test_agente_voz_sesion_sin_configurar_responde_503(monkeypatch):
    """Sin AGENTE_VOZ_TOKEN/ID_PLANTILLA el proxy responde 503 y no intenta red."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    monkeypatch.delenv('AGENTE_VOZ_TOKEN', raising=False)
    monkeypatch.delenv('AGENTE_VOZ_ID_PLANTILLA', raising=False)
    res = TestClient(app).post('/agente-voz/sesion', json={'variables': {}})
    assert res.status_code == 503
    assert res.json()['codigo'] == 'agente_voz_no_configurado'


def test_agente_voz_sesion_proxyea(monkeypatch):
    """Con la config completa, el proxy reenvía url/token/plantilla/variables a agente_voz."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web import server

    seen = {}

    def fake_post(base_url, token, id_plantilla, variables):
        seen.update(base_url=base_url, token=token, id_plantilla=id_plantilla, variables=variables)
        return 201, {'session_id': 'ses_abc', 'ws_url': 'wss://agente.ai-you.io/v1/sesiones/ses_abc?token=tok123'}

    monkeypatch.setenv('AGENTE_VOZ_URL', 'https://agente.ai-you.io/v1/agente-voz')
    monkeypatch.setenv('AGENTE_VOZ_TOKEN', 'tok123')
    monkeypatch.setenv('AGENTE_VOZ_ID_PLANTILLA', '139')
    monkeypatch.setattr(server, '_post_to_agente_voz', fake_post)

    res = TestClient(server.app).post('/agente-voz/sesion', json={'variables': {'lead_id': '1'}})
    assert res.status_code == 201
    assert res.json() == {'session_id': 'ses_abc', 'ws_url': 'wss://agente.ai-you.io/v1/sesiones/ses_abc?token=tok123'}
    assert seen == {
        'base_url': 'https://agente.ai-you.io/v1/agente-voz',
        'token': 'tok123',
        'id_plantilla': 139,
        'variables': {'lead_id': '1'},
    }


def test_agente_voz_sesion_sin_variables_manda_dict_vacio(monkeypatch):
    """Body sin 'variables' (o JSON invalido) no rompe el proxy: manda {}."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web import server

    seen = {}

    def fake_post(base_url, token, id_plantilla, variables):
        seen['variables'] = variables
        return 201, {'session_id': 'ses_abc'}

    monkeypatch.setenv('AGENTE_VOZ_TOKEN', 'tok123')
    monkeypatch.setenv('AGENTE_VOZ_ID_PLANTILLA', '139')
    monkeypatch.setattr(server, '_post_to_agente_voz', fake_post)

    res = TestClient(server.app).post('/agente-voz/sesion', content=b'')
    assert res.status_code == 201
    assert seen['variables'] == {}


def test_models_expone_agente_voz_enabled(monkeypatch):
    """``agente_voz_enabled`` refleja si TOKEN e ID_PLANTILLA están configurados."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    monkeypatch.delenv('AGENTE_VOZ_TOKEN', raising=False)
    monkeypatch.delenv('AGENTE_VOZ_ID_PLANTILLA', raising=False)
    assert TestClient(app).get('/models').json()['agente_voz_enabled'] is False

    monkeypatch.setenv('AGENTE_VOZ_TOKEN', 'tok123')
    monkeypatch.setenv('AGENTE_VOZ_ID_PLANTILLA', '139')
    assert TestClient(app).get('/models').json()['agente_voz_enabled'] is True


def test_static_no_cache_header():
    """``/static/*`` manda Cache-Control: no-cache para evitar JS viejo tras un deploy."""
    from fastapi.testclient import TestClient

    from gemini_live_demo.web.server import app

    res = TestClient(app).get('/static/app.js')
    assert res.status_code == 200
    assert res.headers['cache-control'] == 'no-cache'


def test_bridge_browser_to_gemini():
    """El audio del navegador se reenvia a Gemini y el disconnect cierra el puente."""
    adapter = _FakeAdapter(events=[], block_receive=True)  # Gemini calla
    ws = _FakeWS(incoming=[
        {'type': 'websocket.receive', 'bytes': b'chunk1'},
        {'type': 'websocket.receive', 'bytes': b'chunk2'},
        {'type': 'websocket.disconnect'},
    ])

    asyncio.run(_bridge(ws, adapter))

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
