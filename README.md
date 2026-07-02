# Gemini Live Demo

CLI de voz para probar Gemini Live desde Python.

## Con `uv`

Instalar dependencias:

```bash
uv sync
```

Ejecutar el CLI:

```bash
uv run gemini-live-demo --help
uv run gemini-live-demo list-devices
uv run gemini-live-demo test-mic --seconds 5
uv run gemini-live-demo run
```

## Interfaz web (navegador)

Además del CLI, hay una interfaz web para conversar desde el navegador
(micrófono del propio navegador, sin instalar nada del lado cliente).

```bash
# Arranca el servidor web (FastAPI + WebSocket)
uv run gemini-live-demo serve            # http://127.0.0.1:8000
# o el script dedicado (escucha en 0.0.0.0, usado en Docker):
uv run gemini-live-web
```

Abre `http://127.0.0.1:8000`, aprieta **Iniciar conversación**, permite el
micrófono y habla. El servidor Python nunca expone la `GEMINI_API_KEY`: el
navegador solo intercambia audio por WebSocket.

> **Micrófono y HTTPS:** el navegador solo permite el micrófono en `localhost`
> o bajo **HTTPS**. En producción hay que servir la web por HTTPS (Dokploy lo
> hace automático con Traefik + Let's Encrypt).

### Arquitectura

El mismo *motor* (`gemini_live_demo.core`) alimenta dos "carrocerías":

```
core/  -> motor Gemini (config, audio, events, session, metrics)
cli/   -> terminal: micrófono/altavoz locales (sounddevice)
web/   -> servidor FastAPI + WebSocket (audio desde el navegador)
```

```
[Navegador] --WSS/audio--> [web/server.py (FastAPI)] --> [core] --> [Gemini]
```

## Variables de entorno

```bash
GEMINI_API_KEY=tu_api_key
# Configurable. Alternativa preview (sin validar): gemini-3.1-flash-live-preview
GEMINI_MODEL=gemini-2.5-flash-native-audio-latest
GEMINI_VOICE_NAME=Aoede
GEMINI_LANGUAGE=es-ES
GEMINI_INPUT_SAMPLE_RATE=16000
GEMINI_OUTPUT_SAMPLE_RATE=24000
GEMINI_CHANNELS=1
GEMINI_CHUNK_MS=20
GEMINI_LOG_LEVEL=INFO
```

## Ejemplos

Listar dispositivos:

```bash
uv run gemini-live-demo list-devices
```

Probar microfono:

```bash
uv run gemini-live-demo test-mic --seconds 5
```

Ejecutar demo:

```bash
uv run gemini-live-demo run
```

## Docker

La imagen empaqueta **el servidor web** (no el CLI): usa `uv` para un build
reproducible (misma resolucion que `uv.lock`) y corre como usuario no
privilegiado. Como el audio viaja por WebSocket desde el navegador, la imagen
**no necesita PortAudio ni `/dev/snd`** y corre en cualquier host.

```bash
# Build
docker build -t gemini-live-web .

# Arrancar el servidor (escucha en 0.0.0.0:8000)
docker run --rm -p 8000:8000 --env-file .env gemini-live-web
```

Abre `http://localhost:8000` en el navegador.

Con Compose:

```bash
docker compose up --build
```

## Despliegue en Dokploy

1. Apunta Dokploy al repositorio (build por `Dockerfile`).
2. Configura un **dominio** → Dokploy activa HTTPS automático (Traefik +
   Let's Encrypt). Imprescindible para que el micrófono funcione.
3. Añade `GEMINI_API_KEY` (y demás variables) en **Environment Variables**
   de Dokploy. Nunca en la imagen ni en el repo.
4. El contenedor expone el puerto **8000**; Dokploy enruta el dominio a él.

El WebSocket viaja como `wss://` (seguro) y Traefik proxya el *upgrade* sin
configuración extra.
