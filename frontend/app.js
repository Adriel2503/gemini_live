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
const modelSel = document.getElementById('model');
const callSection = document.getElementById('call-section');
const phoneInput = document.getElementById('phone');
const callBtn = document.getElementById('callBtn');
const tabBrowser = document.getElementById('tabBrowser');
const tabCall = document.getElementById('tabCall');
const panelBrowser = document.getElementById('panel-browser');

// --- Métricas de tokens (panel lateral): historial turno a turno ---
// Gemini manda usage_metadata por turno (no acumulado). Guardamos cada turno
// tal cual llega y además sumamos, para ver tanto el detalle como el total.
const mSumPrompt = document.getElementById('mSumPrompt');
const mSumResponse = document.getElementById('mSumResponse');
const mSumTotal = document.getElementById('mSumTotal');
const turnsEl = document.getElementById('turns');
const turnCountEl = document.getElementById('turnCount');

const sessionTokens = { prompt: 0, response: 0, total: 0 };
let turnCount = 0;

function resetMetrics() {
  sessionTokens.prompt = 0;
  sessionTokens.response = 0;
  sessionTokens.total = 0;
  turnCount = 0;
  mSumPrompt.textContent = mSumResponse.textContent = mSumTotal.textContent = '0';
  turnCountEl.textContent = '0';
  turnsEl.innerHTML = '';
}

const MODALITY_LABEL = { AUDIO: 'audio', TEXT: 'texto', IMAGE: 'imagen', VIDEO: 'video', DOCUMENT: 'doc' };
const MODALITY_CLASS = { AUDIO: 'tm-audio', TEXT: 'tm-text' };

// {AUDIO: 340, TEXT: 130} -> chips HTML "● audio 340" "● texto 130"
function renderModalityChips(byModality) {
  if (!byModality) return '';
  return Object.entries(byModality)
    .map(([modality, count]) => {
      const cls = MODALITY_CLASS[modality] || 'tm-other';
      const label = MODALITY_LABEL[modality] || modality.toLowerCase();
      return `<span class="tm-chip ${cls}"><span class="tm-dot"></span>${label} <b>${count}</b></span>`;
    })
    .join('');
}

function renderModalitySection(promptChips, responseChips) {
  if (!promptChips && !responseChips) return '';
  const rows = [];
  if (promptChips) rows.push(`<div class="turn-modality-row"><span class="tm-label">entrada</span><span class="tm-chips">${promptChips}</span></div>`);
  if (responseChips) rows.push(`<div class="turn-modality-row"><span class="tm-label">salida</span><span class="tm-chips">${responseChips}</span></div>`);
  return `<div class="turn-modality">${rows.join('')}</div>`;
}

function handleUsage(msg) {
  const promptTokens = msg.prompt_tokens ?? 0;
  const responseTokens = msg.response_tokens ?? 0;
  const cachedTokens = msg.cached_tokens ?? 0;
  const totalTokens = msg.total_tokens ?? (promptTokens + responseTokens);
  const promptChips = renderModalityChips(msg.prompt_tokens_by_modality);
  const responseChips = renderModalityChips(msg.response_tokens_by_modality);

  sessionTokens.prompt += promptTokens;
  sessionTokens.response += responseTokens;
  sessionTokens.total += totalTokens;
  mSumPrompt.textContent = sessionTokens.prompt;
  mSumResponse.textContent = sessionTokens.response;
  mSumTotal.textContent = sessionTokens.total;

  turnCount += 1;
  turnCountEl.textContent = turnCount;
  const row = document.createElement('div');
  row.className = 'turn-row';
  row.innerHTML = `
    <div class="turn-num">Turno ${turnCount}</div>
    <div class="turn-stats">
      <span>entrada <b>${promptTokens}</b></span>
      <span>salida <b>${responseTokens}</b></span>
      ${cachedTokens ? `<span>caché <b>${cachedTokens}</b></span>` : ''}
      <span class="t-total">total <b>${totalTokens}</b></span>
    </div>
    ${renderModalitySection(promptChips, responseChips)}`;
  turnsEl.appendChild(row);
  turnsEl.scrollTop = turnsEl.scrollHeight;
}

const MODEL_STORAGE_KEY = 'gemini_live_model';

// --- Pestañas: "Desde el navegador" vs "Por teléfono" son formas distintas
// de probar, no pasos secuenciales, así que solo una está visible a la vez. ---
function selectTab(tab) {
  const showCall = tab === 'call';
  tabBrowser.classList.toggle('active', !showCall);
  tabBrowser.setAttribute('aria-selected', String(!showCall));
  tabCall.classList.toggle('active', showCall);
  tabCall.setAttribute('aria-selected', String(showCall));
  panelBrowser.hidden = showCall;
  callSection.hidden = !showCall;
}

tabBrowser.addEventListener('click', () => selectTab('browser'));
tabCall.addEventListener('click', () => selectTab('call'));

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
  return line;
}

// La transcripcion (Gemini y usuario) llega en fragmentos, no de una vez:
// varios eventos "text"/"user_text" por turno. Se acumulan en la MISMA linea
// hasta que el turno termina (turn_complete/interrupted), asi cada turno
// queda en una sola fila del registro en vez de una fila por fragmento.
let currentAiLine = null;
let currentUserLine = null;

function appendStreamed(text, cls, prefix, getLine, setLine) {
  let line = getLine();
  if (!line) {
    line = log(prefix + text, cls);
    setLine(line);
  } else {
    line.textContent += text;
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function finalizeStreamedLines() {
  currentAiLine = null;
  currentUserLine = null;
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
  const model = encodeURIComponent(modelSel.value || '');
  return `${proto}://${location.host}/ws?model=${model}`;
}

async function loadModels() {
  try {
    const res = await fetch('/models');
    const data = await res.json();
    const saved = localStorage.getItem(MODEL_STORAGE_KEY);
    modelSel.innerHTML = '';
    for (const m of data.models) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label;
      modelSel.appendChild(opt);
    }
    // Preselecciona: última elección guardada (si sigue siendo válida) o el default.
    const valid = data.models.some((m) => m.id === saved);
    modelSel.value = valid ? saved : data.default;
    // La pestaña de llamada telefónica solo aparece si el servidor tiene
    // configurado el bridge de Asterisk (BRIDGE_URL).
    if (data.call_enabled) tabCall.hidden = false;
  } catch (err) {
    modelSel.innerHTML = '<option>Error cargando modelos</option>';
    log('No se pudieron cargar los modelos: ' + err.message, 'log-err');
  }
}

modelSel.addEventListener('change', () => {
  localStorage.setItem(MODEL_STORAGE_KEY, modelSel.value);
});

async function start() {
  resetMetrics();
  finalizeStreamedLines();
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
      if (msg.type === 'text') {
        appendStreamed(msg.text, 'log-ai', 'Gemini: ', () => currentAiLine, (l) => { currentAiLine = l; });
      } else if (msg.type === 'user_text') {
        appendStreamed(msg.text, 'log-sys', 'Tú: ', () => currentUserLine, (l) => { currentUserLine = l; });
      } else if (msg.type === 'interrupted') { stopPlayback(); finalizeStreamedLines(); log('(interrumpido)', 'log-sys'); }
      else if (msg.type === 'turn_complete') { finalizeStreamedLines(); }
      else if (msg.type === 'status' && msg.state === 'ready') log('Sesión de Gemini lista. Ya puedes hablar.', 'log-sys');
      else if (msg.type === 'status' && msg.state === 'go_away') log('El servidor cerró la sesión (límite alcanzado).', 'log-sys');
      else if (msg.type === 'usage') handleUsage(msg);
      else if (msg.type === 'error') log('Error del servidor: ' + msg.message, 'log-err');
      return;
    }
    // Binario: audio PCM16 @ 24 kHz para reproducir.
    playChunk(new Int16Array(event.data));
  };

  ws.onclose = () => { if (running) stop(); };
  ws.onerror = () => log('Error de WebSocket.', 'log-err');

  running = true;
  modelSel.disabled = true;
  log('Modelo: ' + modelSel.value, 'log-sys');
  toggleBtn.textContent = 'Detener';
  toggleBtn.classList.add('on');
}

function stop() {
  running = false;
  modelSel.disabled = false;
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

// --- Llamada telefónica (via bridge de Asterisk) ---

async function makeCall() {
  const number = phoneInput.value.replace(/\D/g, '');
  if (!/^9\d{8}$/.test(number)) {
    log('Número inválido: 9 dígitos empezando en 9 (ej. 987654321).', 'log-err');
    return;
  }
  callBtn.disabled = true;
  phoneInput.disabled = true;
  log(`Llamando al +51 ${number}…`, 'log-sys');
  try {
    const res = await fetch('/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ number, model: modelSel.value || '' }),
    });
    const data = await res.json();
    if (res.ok && data.success) {
      log('Llamada en curso. Contesta el celular y habla con Gemini.', 'log-sys');
    } else {
      log('No se pudo llamar: ' + (data.error || `HTTP ${res.status}`), 'log-err');
    }
  } catch (err) {
    log('Error llamando: ' + err.message, 'log-err');
  } finally {
    callBtn.disabled = false;
    phoneInput.disabled = false;
  }
}

callBtn.addEventListener('click', makeCall);
phoneInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') makeCall(); });

loadModels();
