"""Parsing puro de los eventos que emite la sesion de Gemini Live.

``summarize_event`` normaliza dos formas distintas del mismo evento (dict
crudo del transporte JSON, u objeto tipado del SDK) hacia un unico
``EventSummary``. Es logica pura y determinista: unit-testeable sin red.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _get_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if obj is not None and hasattr(obj, name):
            return getattr(obj, name)
    return None


@dataclass
class EventSummary:
    text: str | None  # transcripcion de la IA (output)
    audio_chunks: list[bytes]
    done: bool
    turn_complete: bool
    generation_complete: bool
    interrupted: bool
    go_away: bool
    model_turn_present: bool
    new_handle: str | None
    resumable: bool | None
    voice_activity_type: str | None = None
    voice_activity_offset: str | None = None
    vad_signal_type: str | None = None
    user_text: str | None = None  # transcripcion del usuario (input)
    # Consumo de tokens del turno (UsageMetadata del SDK). None si el evento
    # no trae metricas (la mayoria no las trae; suele venir con turn_complete).
    prompt_tokens: int | None = None
    response_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None


def summarize_event(event: Any) -> EventSummary:
    text: str | None = None
    user_text: str | None = None
    audio_chunks: list[bytes] = []
    turn_complete = False
    generation_complete = False
    interrupted = False
    go_away = False
    model_turn_present = False
    new_handle: str | None = None
    resumable: bool | None = None
    voice_activity_type: str | None = None
    voice_activity_offset: str | None = None
    vad_signal_type: str | None = None
    prompt_tokens: int | None = None
    response_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None

    if isinstance(event, dict):
        sc = event.get('server_content') or {}
        out = sc.get('output_transcription') or {}
        text = out.get('text') or None
        inp = sc.get('input_transcription') or {}
        user_text = inp.get('text') or None
        turn_complete = bool(sc.get('turn_complete'))
        generation_complete = bool(sc.get('generation_complete'))
        interrupted = bool(sc.get('interrupted'))
        go_away = bool(event.get('go_away'))
        session_update = event.get('session_resumption_update') or {}
        new_handle = session_update.get('new_handle') or session_update.get('newHandle')
        resumable = session_update.get('resumable')
        voice_activity = event.get('voice_activity') or event.get('voiceActivity') or {}
        voice_activity_type = voice_activity.get('voice_activity_type') or voice_activity.get('voiceActivityType')
        voice_activity_offset = voice_activity.get('audio_offset') or voice_activity.get('audioOffset')
        vad_signal = event.get('voice_activity_detection_signal') or event.get('voiceActivityDetectionSignal') or {}
        vad_signal_type = vad_signal.get('vad_signal_type') or vad_signal.get('vadSignalType')
        usage = event.get('usage_metadata') or event.get('usageMetadata') or {}
        prompt_tokens = usage.get('prompt_token_count') or usage.get('promptTokenCount')
        response_tokens = usage.get('response_token_count') or usage.get('responseTokenCount')
        cached_tokens = usage.get('cached_content_token_count') or usage.get('cachedContentTokenCount')
        total_tokens = usage.get('total_token_count') or usage.get('totalTokenCount')
        model_turn = sc.get('model_turn')
        model_turn_present = bool(model_turn)
        return EventSummary(
            text=text,
            audio_chunks=audio_chunks,
            done=turn_complete or generation_complete,
            turn_complete=turn_complete,
            generation_complete=generation_complete,
            interrupted=interrupted,
            go_away=go_away,
            model_turn_present=model_turn_present,
            new_handle=new_handle,
            resumable=resumable,
            voice_activity_type=str(voice_activity_type) if voice_activity_type else None,
            voice_activity_offset=str(voice_activity_offset) if voice_activity_offset else None,
            vad_signal_type=str(vad_signal_type) if vad_signal_type else None,
            user_text=user_text,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
        )

    sc = getattr(event, 'server_content', None)
    if sc is not None:
        out = _get_attr(sc, 'output_transcription', 'outputTranscription')
        if out is not None:
            text = _get_attr(out, 'text') or text
        inp = _get_attr(sc, 'input_transcription', 'inputTranscription')
        if inp is not None:
            user_text = _get_attr(inp, 'text') or user_text
        mt = _get_attr(sc, 'model_turn', 'modelTurn')
        if mt is not None:
            model_turn_present = True
            for part in getattr(mt, 'parts', []) or []:
                inline = _get_attr(part, 'inline_data', 'inlineData')
                if inline is not None:
                    data = _get_attr(inline, 'data')
                    if data:
                        audio_chunks.append(bytes(data))
        turn_complete = bool(_get_attr(sc, 'turn_complete', 'turnComplete'))
        generation_complete = bool(_get_attr(sc, 'generation_complete', 'generationComplete'))
        interrupted = bool(_get_attr(sc, 'interrupted'))

    voice_activity = _get_attr(event, 'voice_activity', 'voiceActivity')
    if voice_activity is not None:
        voice_activity_type = _get_attr(voice_activity, 'voice_activity_type', 'voiceActivityType')
        voice_activity_offset = _get_attr(voice_activity, 'audio_offset', 'audioOffset')
    vad_signal = _get_attr(event, 'voice_activity_detection_signal', 'voiceActivityDetectionSignal')
    if vad_signal is not None:
        vad_signal_type = _get_attr(vad_signal, 'vad_signal_type', 'vadSignalType')

    go_away = bool(_get_attr(event, 'go_away', 'goAway'))
    session_update = _get_attr(event, 'session_resumption_update', 'sessionResumptionUpdate')
    if session_update is not None:
        new_handle = _get_attr(session_update, 'new_handle', 'newHandle')
        resumable = _get_attr(session_update, 'resumable')

    prompt_tokens = response_tokens = cached_tokens = total_tokens = None
    usage = _get_attr(event, 'usage_metadata', 'usageMetadata')
    if usage is not None:
        prompt_tokens = _get_attr(usage, 'prompt_token_count', 'promptTokenCount')
        response_tokens = _get_attr(usage, 'response_token_count', 'responseTokenCount')
        cached_tokens = _get_attr(usage, 'cached_content_token_count', 'cachedContentTokenCount')
        total_tokens = _get_attr(usage, 'total_token_count', 'totalTokenCount')

    return EventSummary(
        text=text,
        audio_chunks=audio_chunks,
        done=turn_complete or generation_complete,
        turn_complete=turn_complete,
        generation_complete=generation_complete,
        interrupted=interrupted,
        go_away=go_away,
        model_turn_present=model_turn_present,
        new_handle=new_handle,
        resumable=resumable,
        user_text=user_text,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
    )
