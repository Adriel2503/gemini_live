"""Servidor web: puente WebSocket entre el navegador y Gemini Live.

Reutiliza el mismo *motor* que el CLI (``core.session``/``core.events``/
``core.config``) pero, en lugar de microfono y altavoz locales, el audio
entra y sale por WebSocket desde el navegador. Por eso este modulo **no
importa** ``sounddevice``/PortAudio: puede correr en un contenedor sin
hardware de audio (ideal para Dokploy).

Protocolo del WebSocket ``/ws``:
  Navegador -> Servidor:
    - binario: PCM16 mono @ input_sample_rate (16 kHz) del microfono.
  Servidor -> Navegador:
    - binario: PCM16 mono @ output_sample_rate (24 kHz) para reproducir.
    - texto JSON: {"type": "status"|"text"|"interrupted"|"turn_complete"|"error", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from gemini_live_demo.core.config import Settings
from gemini_live_demo.core.events import summarize_event
from gemini_live_demo.core.session import GeminiLiveAdapter

logger = logging.getLogger('gemini_live_demo')

# Modelos que el cliente puede elegir desde la UI. Allowlist server-side: el
# navegador no puede pedir un modelo arbitrario (evita typos/abuso).
ALLOWED_MODELS = [
    {'id': 'gemini-2.5-flash-native-audio-latest', 'label': 'Gemini 2.5 Flash · native audio (recomendado)'},
    {'id': 'gemini-3.1-flash-live-preview', 'label': 'Gemini 3.1 Flash Live · preview (sin validar)'},
]
_ALLOWED_MODEL_IDS = {m['id'] for m in ALLOWED_MODELS}


def _find_frontend_dir() -> Path:
    """Localiza la carpeta ``frontend/`` en dev y dentro del contenedor."""
    candidates = []
    env_dir = os.getenv('GEMINI_FRONTEND_DIR')
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path.cwd() / 'frontend')
    # Fallback para layout src/: repo_root/frontend relativo a este archivo.
    candidates.append(Path(__file__).resolve().parents[3] / 'frontend')
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return Path.cwd() / 'frontend'


FRONTEND_DIR = _find_frontend_dir()


def _post_to_bridge(bridge_url: str, token: str, number: str, model: str = '') -> tuple[int, dict]:
    """POST sincrónico al bridge de Asterisk (se corre en un thread).

    Función separada para poder falsearla en tests sin red.
    """
    payload = {'number': number}
    if model:
        payload['model'] = model
    req = urllib.request.Request(
        bridge_url.rstrip('/') + '/call',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'X-Bridge-Token': token},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            payload = {'success': False, 'error': f'bridge respondio HTTP {exc.code}'}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError) as exc:
        return 502, {'success': False, 'error': f'no se pudo contactar al bridge: {exc}'}


async def _bridge(ws: WebSocket, adapter: GeminiLiveAdapter) -> None:
    """Puente bidireccional navegador <-> Gemini para una conexion.

    Corre dos tareas concurrentes (como el bucle continuo del CLI):
      - ``pump_up``: audio del navegador -> Gemini.
      - ``pump_down``: eventos de Gemini -> navegador (audio + estado).
    """
    session_cm = await adapter.connect()
    session = await session_cm.__aenter__()
    adapter.clear_refresh_request()
    logger.info('[web] gemini session ready model=%s', adapter.model)
    await ws.send_json({'type': 'status', 'state': 'ready'})

    stop = asyncio.Event()
    # Estado compartido entre las dos tareas, solo para los logs de validación.
    state = {'last_input_at': None, 'input_logged': False}

    async def pump_up() -> None:
        """Reenvia el audio que llega del navegador hacia Gemini."""
        try:
            while not stop.is_set():
                message = await ws.receive()
                if message.get('type') == 'websocket.disconnect':
                    break
                data = message.get('bytes')
                if data:
                    state['last_input_at'] = time.perf_counter()
                    if not state['input_logged']:  # solo la primera vez
                        state['input_logged'] = True
                        logger.info('[web] audio input started (mic flowing)')
                    await adapter.send_audio_chunk(session, data)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    async def pump_down() -> None:
        """Reenvia el audio/estado de Gemini hacia el navegador."""
        turn_audio_started = False
        try:
            while not stop.is_set():
                saw_event = False
                async for event in adapter.receive(session):
                    saw_event = True
                    if stop.is_set():
                        break
                    summary = summarize_event(event)
                    adapter.note_event(summary)
                    if summary.interrupted:
                        logger.info('[web] interrupted -> flush client playback')
                        await ws.send_json({'type': 'interrupted'})
                    if summary.text:
                        await ws.send_json({'type': 'text', 'text': summary.text})
                    if summary.audio_chunks and not turn_audio_started:
                        # Primer chunk de respuesta del turno: latencia desde el
                        # último audio del usuario (proxy de "tiempo de reacción").
                        turn_audio_started = True
                        if state['last_input_at'] is not None:
                            latency_ms = (time.perf_counter() - state['last_input_at']) * 1000
                            logger.info('[web] audio response started latency_ms=%.0f', latency_ms)
                        else:
                            logger.info('[web] audio response started')
                    for chunk in summary.audio_chunks:
                        await ws.send_bytes(chunk)
                    if summary.generation_complete or summary.turn_complete:
                        await ws.send_json({'type': 'turn_complete'})
                        logger.info('[web] turn complete')
                        turn_audio_started = False
                    if summary.go_away:
                        logger.warning('[web] go_away received; closing session')
                        await ws.send_json({'type': 'status', 'state': 'go_away'})
                        stop.set()
                        break
                if not saw_event:
                    await asyncio.sleep(0.05)
        finally:
            stop.set()

    up = asyncio.create_task(pump_up(), name='web-pump-up')
    down = asyncio.create_task(pump_down(), name='web-pump-down')
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for task in (up, down):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            await session_cm.__aexit__(None, None, None)
        logger.info('[web] gemini session closed')


def create_app() -> FastAPI:
    app = FastAPI(title='Gemini Live Web')

    @app.get('/')
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / 'index.html')

    @app.get('/favicon.ico')
    async def favicon() -> FileResponse:
        return FileResponse(FRONTEND_DIR / 'favicon.svg', media_type='image/svg+xml')

    @app.get('/health')
    async def health() -> dict:
        return {'status': 'ok'}

    @app.get('/models')
    async def models() -> dict:
        """Modelos elegibles + el default (del entorno, si es válido)."""
        env_model = Settings.from_env().model
        default = env_model if env_model in _ALLOWED_MODEL_IDS else ALLOWED_MODELS[0]['id']
        # call_enabled: el frontend muestra la sección "Llamada telefónica"
        # solo si el bridge de Asterisk está configurado en este despliegue.
        return {'models': ALLOWED_MODELS, 'default': default, 'call_enabled': bool(os.getenv('BRIDGE_URL'))}

    @app.post('/call')
    async def call(request: Request) -> JSONResponse:
        """Proxy hacia el bridge de Asterisk: dispara una llamada telefónica.

        El token del bridge vive solo en el servidor (BRIDGE_TOKEN); el
        navegador nunca lo ve. Sin BRIDGE_URL configurado responde 503.
        """
        bridge_url = os.getenv('BRIDGE_URL')
        if not bridge_url:
            return JSONResponse({'success': False, 'error': 'Bridge no configurado (BRIDGE_URL).'}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({'success': False, 'error': 'JSON inválido.'}, status_code=400)
        number = str(body.get('number', '')).strip()
        if not number:
            return JSONResponse({'success': False, 'error': 'Falta el número.'}, status_code=400)
        # Modelo elegido en la web; solo se acepta si está en la allowlist.
        model = str(body.get('model', '')).strip()
        if model and model not in _ALLOWED_MODEL_IDS:
            model = ''
        token = os.getenv('BRIDGE_TOKEN', '')
        status, payload = await asyncio.to_thread(_post_to_bridge, bridge_url, token, number, model)
        logger.info('[web] call proxy number=%s -> bridge status=%d', number, status)
        return JSONResponse(payload, status_code=status)

    @app.websocket('/ws')
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        logger.info('[web] client connected')
        settings = Settings.from_env()
        if not settings.api_key:
            await ws.send_json({'type': 'error', 'message': 'Falta GEMINI_API_KEY en el servidor.'})
            await ws.close()
            return
        # Modelo elegido en la UI (?model=...). Solo se acepta si está en la
        # allowlist; si no, se usa el del entorno.
        requested = ws.query_params.get('model')
        if requested and requested in _ALLOWED_MODEL_IDS:
            settings = replace(settings, model=requested)
        logger.info('[web] session model=%s (requested=%s)', settings.model, requested or 'default')
        adapter = GeminiLiveAdapter(
            api_key=settings.api_key,
            model=settings.model,
            prompt=settings.prompt,
            settings=settings,
            mock=False,
        )
        try:
            await _bridge(ws, adapter)
        except WebSocketDisconnect:
            logger.info('[web] client disconnected')
        except Exception as exc:  # noqa: BLE001 - reportar al cliente y cerrar limpio
            logger.exception('[web] bridge error: %s', exc)
            with suppress(Exception):
                await ws.send_json({'type': 'error', 'message': str(exc)})

    if FRONTEND_DIR.is_dir():
        app.mount('/static', StaticFiles(directory=str(FRONTEND_DIR)), name='static')
    else:
        logger.warning('[web] frontend dir not found at %s', FRONTEND_DIR)

    return app


app = create_app()


def _configure_logging() -> None:
    level_name = os.getenv('GEMINI_LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO), format='%(asctime)s %(levelname)s %(message)s')


def run_server(host: str = '127.0.0.1', port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    _configure_logging()
    logger.info('[web] serving frontend from %s', FRONTEND_DIR)
    if reload:
        uvicorn.run('gemini_live_demo.web.server:app', host=host, port=port, reload=True)
    else:
        uvicorn.run(app, host=host, port=port)


def main() -> None:
    """Entry-point del script ``gemini-live-web`` (usado en Docker/Dokploy)."""
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '8000'))
    run_server(host=host, port=port, reload=False)


if __name__ == '__main__':
    main()
