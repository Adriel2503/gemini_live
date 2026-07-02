"""Adaptador de la sesion de Gemini Live y su doble mock.

``GeminiLiveAdapter`` encapsula el SDK de google-genai: construccion de la
config (voz, VAD, compresion de contexto, resumption) y el envio/recepcion
de eventos. ``MockLiveSession`` permite correr la demo sin red ni API key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from gemini_live_demo.core.audio import (
    chunk_pcm_bytes,
    normalize_language_code,
    summarize_pcm,
)
from gemini_live_demo.core.config import Settings
from gemini_live_demo.core.events import EventSummary

logger = logging.getLogger('gemini_live_demo')


class MockLiveSession:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self._last_input = ''

    async def __aenter__(self) -> 'MockLiveSession':
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def send_realtime_input(self, **kwargs: Any) -> None:
        text = kwargs.get('text')
        if text:
            self._last_input = str(text)
            return
        if kwargs.get('audio') is not None:
            self._last_input = 'audio'

    async def receive(self) -> AsyncIterator[Any]:
        text = f'Modo mock. Ultimo input: {self._last_input or "vacio"}'
        yield {'server_content': {'output_transcription': {'text': text}, 'generation_complete': True}}


class GeminiLiveAdapter:
    def __init__(self, api_key: str, model: str, prompt: str, settings: Settings, mock: bool = False) -> None:
        self.api_key = api_key
        self.model = model
        self.prompt = prompt
        self.settings = settings
        self.mock = mock
        self._client = None
        self._types = None
        self._session_handle: str | None = None
        self._refresh_requested = False

    @property
    def session_handle(self) -> str | None:
        return self._session_handle

    @property
    def should_refresh_session(self) -> bool:
        return self._refresh_requested

    def clear_refresh_request(self) -> None:
        self._refresh_requested = False

    def _load_sdk(self) -> None:
        if self._client is not None:
            return
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=self.api_key)
        self._types = types

    def _build_config(self):
        types = self._types
        modality_audio = getattr(getattr(types, 'Modality', None), 'AUDIO', 'AUDIO')
        voice_config = types.VoiceConfig(
            prebuiltVoiceConfig=types.PrebuiltVoiceConfig(voiceName=self.settings.voice_name)
        )
        speech_config = types.SpeechConfig(
            voiceConfig=voice_config,
            languageCode=normalize_language_code(self.settings.language, self.model),
        )
        compression = types.ContextWindowCompressionConfig(
            triggerTokens=self.settings.context_compression_trigger_tokens,
            slidingWindow=types.SlidingWindow(targetTokens=self.settings.context_compression_target_tokens),
        )
        if self.settings.continuous_mode:
            realtime_input_config = types.RealtimeInputConfig(
                automaticActivityDetection=types.AutomaticActivityDetection(
                    disabled=False,
                    prefixPaddingMs=self.settings.continuous_vad_prefix_ms,
                    silenceDurationMs=self.settings.continuous_vad_silence_ms,
                ),
                activityHandling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turnCoverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            )
        else:
            realtime_input_config = types.RealtimeInputConfig(
                automaticActivityDetection=types.AutomaticActivityDetection(disabled=True),
                activityHandling=types.ActivityHandling.NO_INTERRUPTION,
                turnCoverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            )
        session_resumption = types.SessionResumptionConfig(
            handle=self._session_handle,
        )
        return types.LiveConnectConfig(
            systemInstruction=self.prompt,
            responseModalities=[modality_audio],
            speechConfig=speech_config,
            contextWindowCompression=compression,
            realtimeInputConfig=realtime_input_config,
            sessionResumption=session_resumption,
        )

    async def connect(self):
        if self.mock:
            return MockLiveSession(self.prompt)

        self._load_sdk()
        live_api = getattr(getattr(self._client, 'aio', None), 'live', None)
        if live_api is None or not hasattr(live_api, 'connect'):
            raise RuntimeError('El SDK instalado no expone client.aio.live.connect().')

        config = self._build_config()
        logger.info(
            '[session] opening model=%s resumable_handle=%s',
            self.model,
            'yes' if self._session_handle else 'no',
        )
        return live_api.connect(model=self.model, config=config)

    def note_event(self, summary: EventSummary) -> None:
        if summary.new_handle and summary.new_handle != self._session_handle:
            self._session_handle = summary.new_handle
            logger.info('[session] updated resumption handle resumable=%s', summary.resumable)
        if summary.go_away:
            self._refresh_requested = True
            logger.warning('[session] go_away received; will reconnect after current turn')

    async def send_text(self, session: Any, text: str) -> None:
        if hasattr(session, 'send_realtime_input'):
            await session.send_realtime_input(text=text)
            return
        await session.send(input=text, end_of_turn=True)

    async def send_audio(self, session: Any, pcm_bytes: bytes) -> None:
        duration_seconds, rms, byte_count = summarize_pcm(pcm_bytes, self.settings.input_sample_rate)
        logger.info(
            '[send] audio turn duration=%.2fs bytes=%d rms=%.4f rate=%dHz',
            duration_seconds,
            byte_count,
            rms,
            self.settings.input_sample_rate,
        )
        types = self._types
        chunk_size = self.settings.frames_per_chunk * 2
        chunks_sent = 0
        chunk_delay = self.settings.frames_per_chunk / float(self.settings.input_sample_rate)
        await session.send_realtime_input(activity_start=types.ActivityStart())
        for chunk in chunk_pcm_bytes(pcm_bytes, chunk_size):
            blob = types.Blob(data=chunk, mimeType=f'audio/pcm;rate={self.settings.input_sample_rate}')
            await session.send_realtime_input(audio=blob)
            chunks_sent += 1
            await asyncio.sleep(chunk_delay)
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        logger.info('[send] activity ended chunks=%d chunk_ms=%d', chunks_sent, self.settings.chunk_ms)

    async def send_audio_chunk(self, session: Any, pcm_bytes: bytes) -> None:
        types = self._types
        blob = types.Blob(data=pcm_bytes, mimeType=f'audio/pcm;rate={self.settings.input_sample_rate}')
        await session.send_realtime_input(audio=blob)

    async def receive(self, session: Any) -> AsyncIterator[Any]:
        async for event in session.receive():
            yield event
