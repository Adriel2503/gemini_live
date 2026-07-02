// Cliente de la demo de voz Gemini Live.
//
// Captura el microfono a 16 kHz (PCM16 mono) y lo envia por WebSocket al
// servidor Python, que hace de puente con Gemini. El audio de respuesta llega
// a 24 kHz (PCM16) y se reproduce en streaming. El servidor nunca expone la
// API key: todo pasa por el WebSocket.

const INPUT_RATE = 16000;   // debe coincidir con GEMINI_INPUT_SAMPLE_RATE
const OUTPUT_RATE = 24000;  // debe coincidir con GEMINI_OUTPUT_SAMPLE_RATE

const toggleBtn = document.getElementById('toggle');
const dot = document.getElementById('dot');
const stateEl = document.getElementById('state');
const logEl = document.getElementById('log');

let running = false;
let ws = null;
let micStream = null;
let captureCtx = null;
let processor = null;
let sourceNode = null;

// --- Reproduccion en streaming (24 kHz) ---
let playCtx = null;
let nextTime = 0;
let activeSources = [];

function log(msg, cls = 'log-sys') {
  const line = document.createElement('div');
  line.className = cls;
  line.textContent = msg;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

function setState(text, mode) {
  stateEl.textContent = text;
  dot.className = 'dot' + (mode ? ' ' + mode : '');
}

function floatTo16(input) {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function playChunk(int16) {
  if (!playCtx) return;
  const f32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
  const buffer = playCtx.createBuffer(1, f32.length, OUTPUT_RATE);
  buffer.getChannelData(0).set(f32);
  const src = playCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(playCtx.destination);
  const now = playCtx.currentTime;
  if (nextTime < now) nextTime = now + 0.04;
  src.start(nextTime);
  nextTime += buffer.duration;
  activeSources.push(src);
  setState('Gemini hablando…', 'speaking');
  src.onended = () => {
    activeSources = activeSources.filter((s) => s !== src);
    if (activeSources.length === 0 && running) setState('Escuchando…', 'live');
  };
}

function stopPlayback() {
  activeSources.forEach((s) => { try { s.stop(); } catch (e) {} });
  activeSources = [];
  nextTime = 0;
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws`;
}

async function start() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    log('No se pudo acceder al micrófono: ' + err.message, 'log-err');
    log('Recuerda: el micrófono solo funciona en HTTPS (o localhost).', 'log-err');
    return;
  }

  playCtx = new (window.AudioContext || window.webkitAudioContext)();
  await playCtx.resume();

  // Contexto de captura fijado a 16 kHz (el navegador resamplea el micro).
  captureCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: INPUT_RATE });
  await captureCtx.resume();

  ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    log('Conectado al servidor.', 'log-sys');
    setState('Escuchando…', 'live');
    sourceNode = captureCtx.createMediaStreamSource(micStream);
    processor = captureCtx.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (e) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const pcm = floatTo16(e.inputBuffer.getChannelData(0));
      ws.send(pcm.buffer);
    };
    sourceNode.connect(processor);
    processor.connect(captureCtx.destination); // requerido para que corra (sale silencio)
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      const msg = JSON.parse(event.data);
      if (msg.type === 'text') log('Gemini: ' + msg.text, 'log-ai');
      else if (msg.type === 'interrupted') { stopPlayback(); log('(interrumpido)', 'log-sys'); }
      else if (msg.type === 'turn_complete') { /* fin de turno */ }
      else if (msg.type === 'status' && msg.state === 'ready') log('Sesión de Gemini lista. Ya puedes hablar.', 'log-sys');
      else if (msg.type === 'status' && msg.state === 'go_away') log('El servidor cerró la sesión (límite alcanzado).', 'log-sys');
      else if (msg.type === 'error') log('Error del servidor: ' + msg.message, 'log-err');
      return;
    }
    // Binario: audio PCM16 @ 24 kHz para reproducir.
    playChunk(new Int16Array(event.data));
  };

  ws.onclose = () => { if (running) stop(); };
  ws.onerror = () => log('Error de WebSocket.', 'log-err');

  running = true;
  toggleBtn.textContent = 'Detener';
  toggleBtn.classList.add('on');
}

function stop() {
  running = false;
  toggleBtn.textContent = 'Iniciar conversación';
  toggleBtn.classList.remove('on');
  setState('Desconectado', null);

  stopPlayback();
  if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
  if (sourceNode) { sourceNode.disconnect(); sourceNode = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (captureCtx) { captureCtx.close(); captureCtx = null; }
  if (playCtx) { playCtx.close(); playCtx = null; }
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  log('Conversación detenida.', 'log-sys');
}

toggleBtn.addEventListener('click', () => {
  if (running) stop();
  else start();
});
