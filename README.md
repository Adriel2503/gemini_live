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
