# Gemini Live Demo

Asistente de voz con Gemini Live: CLI de terminal, interfaz web (micrófono
del navegador), y **llamadas telefónicas reales** vía un bridge de Asterisk
en Go (repo aparte, `asterisk-bridge`).

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

Abre `http://127.0.0.1:8000`, elige el **modelo** en el desplegable, aprieta
**Iniciar conversación**, permite el micrófono y habla. El servidor Python
nunca expone la `GEMINI_API_KEY`: el navegador solo intercambia audio por
WebSocket.

El modelo se elige por sesión desde la UI (se recuerda en el navegador); el
`GEMINI_MODEL` del entorno queda como default. La lista de modelos elegibles
está en `ALLOWED_MODELS` (allowlist server-side en `web/server.py`).

La UI tiene dos paneles independientes: **"Desde el navegador"** (mic local)
y **"Por teléfono"** (dispara una llamada real, ver siguiente sección). El
panel de teléfono solo aparece si el servidor tiene `BRIDGE_URL` configurado.

> **Micrófono y HTTPS:** el navegador solo permite el micrófono en `localhost`
> o bajo **HTTPS**. En producción hay que servir la web por HTTPS (Dokploy lo
> hace automático con Traefik + Let's Encrypt).

## Llamadas telefónicas (bridge de Asterisk)

El botón **"Llamar"** de la web dispara una llamada telefónica real: Gemini
conversa con quien atiende, usando el mismo motor (`core/session.py`) que la
web y el CLI. El flujo completo:

```
Navegador --POST /call {number,model}--> este servidor (proxy) --HTTP--> asterisk-bridge (Go)
                                                                              │ AMI Originate
                                                                              ▼
                                                          Celular <--PJSIP-- Asterisk <--AudioSocket--> bridge <--WebSocket /ws--> este servidor <--> Gemini
```

- El servidor Python actúa de **proxy**: `POST /call` (`web/server.py`)
  reenvía el pedido al bridge de Asterisk (repo separado, en Go) agregando
  el `BRIDGE_TOKEN` — el navegador nunca ve ese token.
- El bridge de Go se conecta de vuelta a este mismo servidor por el `/ws`
  (el mismo endpoint WebSocket que usa el navegador) para puentear el audio
  de la llamada con Gemini.
- Sin `BRIDGE_URL` configurado, el endpoint `/call` responde `503` y el
  panel "Por teléfono" ni siquiera aparece en la UI (`GET /models` expone
  `call_enabled`).

Variables necesarias (van en el `.env` de **este** servidor, no en el
bridge):

```bash
BRIDGE_URL=http://<ip-del-servidor-asterisk>:9094
BRIDGE_TOKEN=<mismo-token-que-BRIDGE_TOKEN-en-asterisk-bridge>
```

El detalle de cómo el bridge de Go arma el stream de audio (por qué rellena
silencio, cómo detecta Gemini el fin del turno, etc.) está documentado en el
repo `asterisk-bridge`, en `docs/audio-pipeline.md`.

## Que Gemini hable primero (saludo inicial)

Por default, la conversación espera a que hable el usuario (o quien atiende
la llamada). Se puede invertir eso para que **Gemini salude primero** apenas
se abre la sesión:

```bash
GEMINI_GREET_FIRST=true
GEMINI_GREETING_TRIGGER=[Inicio de sesion: saluda brevemente y pregunta en que puedes ayudar]
```

Internamente esto **no** es una instrucción de comportamiento (eso ya lo
hace el `prompt`/`systemInstruction`) — es un turno de **texto** que se
manda apenas se abre la sesión (`GeminiLiveAdapter.greet()`), disparando una
respuesta real sin depender del VAD (que solo reacciona a audio). Aplica
igual en la web, el CLI y las llamadas telefónicas, porque los tres
consumidores comparten el mismo `core/session.py`. En reconexiones dentro de
la misma sesión (`session_resumption`) no se repite el saludo.

## Transcripción (texto del usuario y de la IA)

Activado por default (`GEMINI_TRANSCRIBE=true`): la Live API transcribe
tanto lo que dice el usuario como lo que responde Gemini, sin costo extra de
tokens en la transcripción de entrada.

- El servidor manda el texto del usuario como `{"type":"user_text"}` y el de
  la IA como `{"type":"text"}` por el WebSocket.
- La web lo muestra en pantalla como "Tú: ..." / la respuesta de la IA.
- En las **llamadas telefónicas**, el bridge de Go loguea el diálogo
  completo por UUID de llamada (`USUARIO: ...` / `IA: ...`), útil para
  revisar qué se dijo en una llamada desde `journalctl` sin grabación de
  audio.

Poner `GEMINI_TRANSCRIBE=false` para desactivarlo.

### Arquitectura

El mismo *motor* (`gemini_live_demo.core`) alimenta tres "carrocerías":

```
core/  -> motor Gemini (config, audio, events, session, metrics)
cli/   -> terminal: micrófono/altavoz locales (sounddevice)
web/   -> servidor FastAPI + WebSocket (navegador Y bridge de Asterisk)
```

```
[Navegador] ──WSS/audio──┐
                          ├──> [web/server.py (FastAPI, /ws)] ──> [core] ──> [Gemini Live API]
[asterisk-bridge (Go)] ──┘         (mismo endpoint /ws para ambos)
```

El bridge de Go **no habla directo con Gemini** — es un cliente más de este
`/ws`, igual que el navegador. Detalle completo en el README y
`docs/audio-pipeline.md` del repo `asterisk-bridge`.

## Variables de entorno

Lista completa (ver también `.env.example`, con comentarios por sección):

```bash
# Gemini API
GEMINI_API_KEY=tu_api_key
GEMINI_MODEL=gemini-2.5-flash-native-audio-latest
GEMINI_VOICE_NAME=Aoede
GEMINI_LANGUAGE=es-US

# Audio
GEMINI_INPUT_SAMPLE_RATE=16000
GEMINI_OUTPUT_SAMPLE_RATE=24000
GEMINI_CHANNELS=1
GEMINI_CHUNK_MS=20

# Deteccion de voz (VAD, modo continuo)
GEMINI_CONTINUOUS_MODE=true
GEMINI_VAD_SILENCE_MS=700   # cuanto silencio tolera antes de cerrar el turno
GEMINI_VAD_PREFIX_MS=200    # audio "hacia atras" que retiene el VAD al detectar el inicio del habla

# Saludo inicial (Gemini habla primero)
GEMINI_GREET_FIRST=false
GEMINI_GREETING_TRIGGER=[Inicio de sesion: saluda brevemente y pregunta en que puedes ayudar]

# Transcripcion (texto del usuario y de la IA)
GEMINI_TRANSCRIBE=true

# Llamadas telefonicas (bridge de Asterisk, opcional)
BRIDGE_URL=http://ip-del-servidor-asterisk:9094
BRIDGE_TOKEN=CHANGE_ME

# Cola de audio (diagnostico, CLI)
GEMINI_AUDIO_QUEUE_MAX_CHUNKS=200
GEMINI_AUDIO_QUEUE_LOG_EVERY_CHUNKS=250
GEMINI_AUDIO_QUEUE_WARN_CHUNKS=25

# Metricas / grabacion (CLI)
GEMINI_METRICS_CSV=true
GEMINI_METRICS_DIR=metrics
GEMINI_MIN_RECORD_SECONDS=0.5
GEMINI_SILENCE_RMS_THRESHOLD=0.01

# Compresion de contexto (sesiones largas)
GEMINI_CONTEXT_TRIGGER_TOKENS=16000
GEMINI_CONTEXT_TARGET_TOKENS=8000

GEMINI_LOG_LEVEL=INFO
```

El system prompt **no** se configura por variable simple: vive hardcodeado
en `core/config.py` (`Settings.prompt`). Definir `GEMINI_SYSTEM_PROMPT` solo
si hace falta sobreescribirlo completo.

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
3. Añade `GEMINI_API_KEY` (y demás variables, incluidas `BRIDGE_URL`/
   `BRIDGE_TOKEN` si aplica) en **Environment Variables** de Dokploy. Nunca
   en la imagen ni en el repo.
4. El contenedor expone el puerto **8000**; Dokploy enruta el dominio a él.

El WebSocket viaja como `wss://` (seguro) y Traefik proxya el *upgrade* sin
configuración extra.
