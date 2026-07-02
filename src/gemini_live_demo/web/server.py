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
import logging
import os
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gemini_live_demo.core.config import Settings
from gemini_live_demo.core.events import summarize_event
from gemini_live_demo.core.session import GeminiLiveAdapter

logger = logging.getLogger('gemini_live_demo')


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


async def _bridge(ws: WebSocket, adapter: GeminiLiveAdapter, settings: Settings) -> None:
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

    async def pump_up() -> None:
        """Reenvia el audio que llega del navegador hacia Gemini."""
        try:
            while not stop.is_set():
                message = await ws.receive()
                if message.get('type') == 'websocket.disconnect':
                    break
                data = message.get('bytes')
                if data:
                    await adapter.send_audio_chunk(session, data)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    async def pump_down() -> None:
        """Reenvia el audio/estado de Gemini hacia el navegador."""
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
                    for chunk in summary.audio_chunks:
                        await ws.send_bytes(chunk)
                    if summary.generation_complete or summary.turn_complete:
                        await ws.send_json({'type': 'turn_complete'})
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

    @app.websocket('/ws')
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        settings = Settings()
        if not settings.api_key:
            await ws.send_json({'type': 'error', 'message': 'Falta GEMINI_API_KEY en el servidor.'})
            await ws.close()
            return
        adapter = GeminiLiveAdapter(
            api_key=settings.api_key,
            model=settings.model,
            prompt=settings.prompt,
            settings=settings,
            mock=False,
        )
        try:
            await _bridge(ws, adapter, settings)
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
