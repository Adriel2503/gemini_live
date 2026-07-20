# Diseño: Motor Gemini Live en agente_voz (MVP sin tools)

> Objetivo: reemplazar Ultravox por Gemini Live como motor de voz del gateway
> `agente_voz` (Node), portando la lógica ya validada en producción del
> proyecto Python (`gemini_live`) y del puente Go (`asterisk-bridge`).
> Alcance MVP: conversación + transcripción + barge-in + saludo inicial.
> **Fuera de alcance (Fase 2): tools** (tipificar, agendar, buscarSucursal,
> queryCorpus) — la llamada conversa pero no ejecuta acciones ni tipifica.

---

## 1. Decisiones tomadas

| Decisión | Valor | Racional |
|---|---|---|
| Estrategia de motor | **Full Gemini** vía env `ENGINE=gemini` (default `ultravox`) | El usuario deja Ultravox. Sin migración de DB; el código Ultravox queda intacto como kill-switch: `ENGINE=ultravox` revierte sin deploy. |
| API key | **`GEMINI_API_KEY` global** (env del gateway) | Una key para todas las empresas en el MVP. ✅ Rotada — la key pegada en chats anteriores ya no está en uso. |
| Flag en DB | **Ninguno** | `empresa.ultravox_api_key`, keys adicionales y tabla `voz` (ElevenLabs) quedan sin uso con Gemini. No se borran. Trazabilidad del motor: se agrega `motor: "gemini"` dentro de `metadata` (jsonb) en `upsertSesion` — sin cambio de schema. |
| Límite de canales | Se **reusa `empresa.canal`** | Con Gemini no hay pool de api keys; se cuenta sesiones vivas por empresa usando `apiKey = "gemini:{idEmpresa}"` como clave sintética en `store.contarPorApiKey()` (no colisiona entre empresas). |
| Voz | `GEMINI_VOICE` global (default `Aoede`) | La tabla `voz` es de ElevenLabs/Ultravox. En MVP se ignora `id_voz`; Fase 2 puede mapear `voz.provider='gemini'`. |
| SDK | **`@google/genai`** (`ai.live.connect`) | Mismo SDK familia que el Python validado; mismos nombres de config (camelCase). Plan B si el SDK falla: WS crudo a `BidiGenerateContent` (el protocolo ya lo conocemos). |
| Modelo | `GEMINI_MODEL` default `gemini-3.1-flash-live-preview` | Es el que usamos para telefonía; transcripción más confiable que 2.5 native-audio. |

---

## 2. Arquitectura

```
                       agente_voz (Node)
   Cliente (Asterisk         │
   3ro / navegador)          │
        │  WS /v1/sesiones/:id?token=
        ▼
  ┌─────────────────────────────────────────────────────────┐
  │ index.js (upgrade + auth)          [SIN CAMBIOS]        │
  │ sesiones.controller.js  ──switch ENGINE──┐  [1 punto]   │
  │        │ ultravox.service.js (queda)     │              │
  │        │ gemini.service.js  ◄────────────┘  [NUEVO]     │
  │        ▼                                                │
  │ audioBridge.js ──if sesion.engine──► geminiEngine.js    │
  │   [rama nueva]        [NUEVO: adaptador + bombas]       │
  │                             │                           │
  │      lib/resample.js  lib/audioQueue.js   [NUEVOS,      │
  │      (port del Go)    (port del Go)        puros]       │
  └─────────────────────────────┼───────────────────────────┘
                                │ @google/genai live.connect
                                ▼
                        Gemini Live API
                     (sube PCM16 16kHz / baja PCM16 24kHz)
```

### Flujo de audio (telefónico, `codec=mulaw_8k`)

```
SUBIDA  (caller → Gemini), ticker 20 ms:
  asteriskWs binario mulaw ─► muLawToPcm16 (8k, 320B) ─► inQ.push()
  cada tick: frame = inQ.popFrame(320) ?? SILENCIO_320   ← silence-fill ⚠️
             upsample8to16(frame) ─► session.sendRealtimeInput(audio 16k)

BAJADA  (Gemini → caller), ticker 20 ms:
  onmessage audio 24k ─► downsampler24a8.process() (carry) ─► outQ.push()
  cada tick: frame = outQ.popFrame(320); si null → nada (no silence-fill)
             pcm16ToMuLaw(frame) ─► asteriskWs.send()

BARGE-IN: evento interrupted → outQ.clear() + {type:"playback_clear_buffer"} al cliente
```

Para `codec=pcm_s16le_16k` (navegador): subida directa (ya está a 16k,
frames de 640B, silencio de 640B); bajada con `Downsampler24a16` (3→2 con carry).

**Regla de oro (del fix Go): la subida SIEMPRE manda 50 frames/s** —
si el cliente calla o el trunk suprime silencio, se manda silencio digital.
Sin esto el VAD de Gemini nunca cierra el turno (bug ya vivido y resuelto).

---

## 3. Archivos

### Nuevos

| Archivo | Responsabilidad |
|---|---|
| `src/services/gemini.service.js` | Contrato de motor (espejo de ultravox.service). |
| `src/ws/geminiEngine.js` | Adaptador: abre `ai.live.connect`, normaliza eventos, bombas con pacing. |
| `src/lib/resample.js` | Port de `resample.go`: `upsample8to16`, `Downsampler24a8`, `Downsampler24a16` (con carry). Funciones puras + clases con estado por sesión. |
| `src/lib/audioQueue.js` | Port de `audioQueue` Go: buffer plano + `head` offset + compactación; `push/popFrame/clear`. Sin mutex (Node single-thread). |
| `test/resample.test.js`, `test/audioQueue.test.js` | Unit tests (`node:test`), portando los casos del Go. |

### Modificados

| Archivo | Cambio |
|---|---|
| `src/config/env.js` | Bloque `env.engine` + `env.gemini` (ver §7). |
| `src/controllers/sesiones.controller.js` | 3 toques: (a) si `ENGINE=gemini` NO exigir `ultravox_api_key`; (b) candidato único `{apiKey:"gemini:"+idEmpresa, canal: empresa.canal}` en vez del pool de keys; (c) `switch` en la llamada al motor (línea ~165) → `gemini.crearLlamadaServerWs(...)`; catch usa `clasificarError` del motor activo. |
| `src/ws/audioBridge.js` | Al inicio de `manejarConexion`: `if (sesion.engine === "gemini") return geminiEngine.manejarConexion(asteriskWs, sesion)`. El camino Ultravox no se toca. |
| `package.json` | + `@google/genai`. |
| `.env.example` | Documentar las vars nuevas. |

---

## 4. Contrato `gemini.service.js`

Mismas firmas que ultravox.service para no tocar el controller más de lo mínimo:

```js
// crearLlamadaServerWs: NO llama a ninguna API (Gemini no tiene paso HTTP previo).
// Valida GEMINI_API_KEY, genera callId propio y devuelve la config que el
// engine usará al conectar. joinUrl no existe en Gemini → null.
async function crearLlamadaServerWs({ apiKey, systemPrompt, voice, sampleRate,
                                      selectedTools = [], languageHint, velocidad }) {
  // → { callId: "gem_<uuid>", joinUrl: null,
  //     geminiConfig: { model, systemPrompt, voice, sampleRate } }
  // selectedTools se IGNORA en MVP (Fase 2).
}

// sendDataMessage: inyectar texto a la sesión viva. Sin REST: resuelve la
// sesión del store y llama sesion.engineEnviarTexto(text) (expuesto por el engine).
async function sendDataMessage(apiKey, callId, { text })

// clasificarError: misma convención → "caido" | "rechazado" | null
// (401/403/429 de Google → "rechazado"; red/5xx → "caido")
function clasificarError(error)
```

El controller guarda en el store: `engine: "gemini"`, `callId`, `geminiConfig`.
`ws_url` público al cliente **no cambia** (mismo contrato externo del gateway).

---

## 5. `geminiEngine.js` — el adaptador (corazón del diseño)

### 5.1 Conexión (port de `session.py::_build_config`)

```js
const { GoogleGenAI, Modality } = require("@google/genai");
const ai = new GoogleGenAI({ apiKey: env.gemini.apiKey });

const session = await ai.live.connect({
  model: env.gemini.model,
  config: {
    systemInstruction: sesion.geminiConfig.systemPrompt,     // prompt multi-empresa ya renderizado
    responseModalities: [Modality.AUDIO],
    speechConfig: {
      voiceConfig: { prebuiltVoiceConfig: { voiceName: env.gemini.voice } }, // Aoede
      languageCode: normalizarLenguaje(env.gemini.language, env.gemini.model), // es→es-US si native-audio
    },
    realtimeInputConfig: {
      automaticActivityDetection: {
        disabled: false,
        prefixPaddingMs: env.gemini.vadPrefixMs,      // 200
        silenceDurationMs: env.gemini.vadSilenceMs,   // 500 (validado en telefonía)
      },
      activityHandling: "START_OF_ACTIVITY_INTERRUPTS",
      turnCoverage: "TURN_INCLUDES_ONLY_ACTIVITY",
    },
    inputAudioTranscription: {},                       // transcripción usuario
    outputAudioTranscription: {},                      // transcripción IA
    contextWindowCompression: {
      triggerTokens: 16000, slidingWindow: { targetTokens: 8000 },
    },
  },
  callbacks: { onopen, onmessage, onerror, onclose },  // SDK JS = callbacks, no async-for
});
```

Saludo inicial (port de `greet()`): tras `onopen`, si `env.gemini.greetFirst`
→ `session.sendRealtimeInput({ text: env.gemini.greetingTrigger })` (texto,
esquiva el VAD; una sola vez).

### 5.2 Envío de audio (subida)

```js
session.sendRealtimeInput({
  audio: { data: pcm16k.toString("base64"), mimeType: "audio/pcm;rate=16000" },
});
```

### 5.3 Parseo de eventos (`onmessage`, port de `events.py`)

| Del mensaje Gemini | Acción |
|---|---|
| `serverContent.modelTurn.parts[].inlineData.data` (base64) | audio 24k → downsampler → `outQ.push` |
| `serverContent.outputTranscription.text` | acumular en `turnoIA`; emitir `transcript_partial` (role agent) |
| `serverContent.inputTranscription.text` | acumular en `turnoUser`; emitir `transcript_partial` (role user) |
| `serverContent.interrupted` | `outQ.clear()` + `playback_clear_buffer` al cliente + finalizar `turnoIA` |
| `serverContent.turnComplete \|\| generationComplete` | emitir `transcript_final` (agent) + `agent_stopped_speaking` |
| primer audio del turno | emitir `agent_started_speaking` |
| `goAway` | cerrar con motivo `gemini_go_away` |
| `sessionResumptionUpdate.newHandle` | guardar en sesión (log; reconexión = Fase 2) |

**Protocolo hacia el cliente NO cambia**: se emiten los mismos JSON que hoy
(`transcript_partial/final`, `agent_started/stopped_speaking`,
`playback_clear_buffer`, `pong`) — el integrador telefónico no nota el cambio de motor.

Transcripción persistente: cada `transcript_final` se apendea a
`sesion.transcripcion[]` en el store → `GET /transcripcion` la sirve desde
memoria (Gemini no tiene REST de mensajes como Ultravox).

### 5.4 Mensajes del cliente (mismo switch que hoy)

| `ctrl.type` | Acción |
|---|---|
| binario | mulaw→PCM si aplica → `inQ.push` |
| `session_end` | cierre directo (sin gracia de tipificación: no hay tools en MVP) |
| `ping` | `pong` |
| `user_text` | `session.sendRealtimeInput({ text })` |
| `dtmf` | no-op (como hoy) |

### 5.5 Ciclo de vida y cierre (port del cascade Go)

- Estado compartido `cerrado` + función `cerrar(motivo)` **idempotente**.
- `cerrar()` SIEMPRE: `clearInterval` de ambos tickers, `session.close()` (Gemini),
  `asteriskWs.close()`, `store.actualizar(estado:"finalizada")`,
  `upsertSesion(estado:"ended", metadata.motor:"gemini")`, webhook `session.ended`,
  `store.eliminar`. (Lección de producción: socket sin cerrar = fuga de FDs.)
- Dispara cierre: close/error de cualquiera de los dos lados, `goAway`,
  `session_end`, timeout `MAX_CALL_SECONDS` (nuevo, default 300 s, red de seguridad del Go).
- Contadores de diagnóstico (del Go): `framesUp, silenceUp, audioMsgsDown,
  bytesDown, framesWritten` → log RESUMEN al cerrar.

---

## 6. `lib/resample.js` y `lib/audioQueue.js` (ports exactos del Go)

- `upsample8to16(buf)`: x2, interpolación lineal (`out[2i]=in[i]`,
  `out[2i+1]=(in[i]+in[i+1])/2`, última repetida). Int16 LE, aritmética en 32 bits.
- `class Downsampler24a8`: grupos de 6 bytes (3 samples) → promedio; `carry`
  de 0–5 bytes entre chunks (imprescindible: los chunks de Gemini no vienen alineados).
  **Una instancia por sesión.**
- `class Downsampler24a16`: grupos de 3 samples → 2 samples (`s0`, `(s1+s2)/2`), con carry.
- `class AudioQueue`: `buf` + `head`; `push` compacta cuando el prefijo muerto
  pesa más que lo pendiente; `popFrame(n)` devuelve exactamente `n` bytes o `null`;
  `clear()` = barge-in. Sin locks.
- Optimización de memoria (patrones del Go): `SILENCIO` alocado una vez por
  módulo/sesión; buffers chicos por frame están OK (young-gen del GC).

---

## 7. Configuración (`env.js` + `.env.example`)

```ini
# Motor
ENGINE=gemini                        # gemini | ultravox (kill-switch)

# Gemini Live
GEMINI_API_KEY=                      # ✅ key ya rotada (la expuesta en chat quedo invalidada)
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_VOICE=Aoede
GEMINI_LANGUAGE=es-ES                # se normaliza a es-US en native-audio
GEMINI_VAD_SILENCE_MS=500
GEMINI_VAD_PREFIX_MS=200
GEMINI_TRANSCRIBE=1
GEMINI_GREET_FIRST=1
GEMINI_GREETING_TRIGGER=[Inicio de sesion: saluda segun tu guion]
MAX_CALL_SECONDS=300
```

---

## 8. Plan de verificación

1. **Unit** (`node --test`): resample (casos del Go: tamaños, carry con chunks
   desalineados, drift) y audioQueue (FIFO, pop parcial, clear, compactación).
2. **Smoke local sin telefonía**: script `scripts/smoke-gemini.js` que abre el
   WS público del gateway con `codec=pcm_s16le_16k`, manda un WAV 16k de prueba
   (el patrón `--probe` que ya usamos en Go) y verifica: `ready` → audio de
   vuelta → `transcript_final`.
3. **Llamada real**: crear sesión desde la plataforma del usuario → verificar
   greet, conversación, barge-in (interrumpir a la IA), transcripción en logs.
4. **Cortes**: colgar el teléfono a mitad de respuesta → sin sesión zombie
   (log RESUMEN + `session.ended`); matar la sesión Gemini → el cliente recibe cierre.
5. **Kill-switch**: `ENGINE=ultravox` → el flujo viejo sigue intacto.

## 9. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| SDK `@google/genai` live con comportamiento distinto al Python | Config idéntica (camelCase igual); plan B: WS crudo BidiGenerate (protocolo conocido). |
| Sin tools → **no tipifica** la llamada | Aceptado explícitamente para el MVP; Fase 2 = ejecutor de tools (`toolCall`→fetch→`toolResponse`). |
| `queryCorpus` (RAG Ultravox) sin equivalente | Fuera de alcance; documentado. |
| Ráfagas de audio de Gemini (chunks grandes) | Paced writer 20 ms + outQ (port Go); límite de cola (cap) para no crecer sin fin. |
| Key global expuesta previamente | ✅ Rotada. |

## 10. Orden de implementación sugerido (para /sc:implement)

1. `lib/audioQueue.js` + `lib/resample.js` + tests (puros, sin riesgo).
2. `config/env.js` + `.env.example` + dependencia `@google/genai`.
3. `services/gemini.service.js` (contrato).
4. `ws/geminiEngine.js` (adaptador + bombas + cierre).
5. Toques en `sesiones.controller.js` y `audioBridge.js` (branch).
6. `scripts/smoke-gemini.js` + prueba end-to-end.
