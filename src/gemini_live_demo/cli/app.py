"""Punto de entrada CLI de la demo de voz para Gemini Live.

Este modulo es intencionalmente delgado: define los comandos Typer y la
configuracion de logging. Toda la logica vive en modulos dedicados
(config, audio, events, audio_io, session, runner, metrics).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import time
from dataclasses import replace

import numpy as np
import sounddevice as sd
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress

from gemini_live_demo.cli.audio_io import list_devices
from gemini_live_demo.core.config import Settings
from gemini_live_demo.cli.runner import DemoRunner
from gemini_live_demo.core.session import GeminiLiveAdapter

console = Console()
logger = logging.getLogger('gemini_live_demo')
app = typer.Typer(add_completion=False, help='Demo de voz para Gemini Live.')


def configure_logging() -> None:
    level_name = os.getenv('GEMINI_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(message)s',
        datefmt='[%X]',
        handlers=[RichHandler(rich_tracebacks=True, markup=True)],
        force=True,
    )
    logger.setLevel(level)


@app.command('list-devices')
def list_devices_cmd() -> None:
    configure_logging()
    list_devices()


@app.command('test-mic')
def test_mic_cmd(seconds: int = typer.Option(5, min=1, help='Duracion en segundos.')) -> None:
    configure_logging()
    settings = Settings.from_env()
    frames: list[np.ndarray] = []
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, frames_count, time_info, status):  # noqa: ANN001
        if status:
            console.print(f'[yellow]{status}[/yellow]')
        q.put(indata.copy())

    console.print(f'Grabando {seconds}s desde el microfono...')
    with sd.InputStream(
        samplerate=settings.input_sample_rate,
        channels=settings.channels,
        dtype='float32',
        callback=callback,
    ):
        start = time.perf_counter()
        with Progress() as progress:
            task = progress.add_task('capturando', total=seconds)
            while time.perf_counter() - start < seconds:
                try:
                    frames.append(q.get(timeout=0.1))
                except queue.Empty:
                    pass
                progress.update(task, completed=time.perf_counter() - start)

    if not frames:
        console.print('[red]No se capturo audio.[/red]')
        return

    audio = np.concatenate(frames, axis=0)
    rms = float(np.sqrt(np.mean(np.square(audio))))
    console.print(f'[green]Audio capturado[/green] frames={len(audio)} rms={rms:.4f}')


@app.command()
def run(
    mock: bool = typer.Option(False, help='Usa una sesion mock local.'),
    manual: bool = typer.Option(False, help='Usa Enter para grabar/enviar cada turno en vez de streaming continuo.'),
    no_playback: bool = typer.Option(False, help='Diagnostico: recibe audio de Gemini pero no lo reproduce localmente.'),
    record_output_wav: bool = typer.Option(False, help='Diagnostico: guarda el audio de Gemini en WAV sin reproducirlo.'),
) -> None:
    configure_logging()
    settings = Settings.from_env()
    if manual:
        settings = replace(settings, continuous_mode=False)
    if no_playback:
        settings = replace(settings, no_playback=True)
    if record_output_wav:
        settings = replace(settings, record_output_wav=True, no_playback=True)
    if not mock and not settings.api_key:
        raise typer.BadParameter('Falta GEMINI_API_KEY en el entorno o en .env')

    adapter = GeminiLiveAdapter(
        api_key=settings.api_key or 'mock',
        model=settings.model,
        prompt=settings.prompt,
        settings=settings,
        mock=mock,
    )
    asyncio.run(DemoRunner(adapter, settings).run())


@app.command()
def serve(
    host: str = typer.Option('127.0.0.1', help='Host donde escucha el servidor web.'),
    port: int = typer.Option(8000, help='Puerto del servidor web.'),
    reload: bool = typer.Option(False, help='Recarga en caliente (desarrollo).'),
) -> None:
    """Arranca la interfaz web (FastAPI + WebSocket) para conversar desde el navegador."""
    from gemini_live_demo.web.server import run_server

    console.print(f'Servidor web en http://{host}:{port}  (Ctrl+C para salir)')
    run_server(host=host, port=port, reload=reload)


if __name__ == '__main__':
    app()
