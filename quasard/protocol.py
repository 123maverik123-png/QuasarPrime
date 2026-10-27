"""Сборка JSON-сообщений протокола uniproxy (wss://uniproxy.alice.yandex.net/uni.ws).

Форматы сняты с реального трафика Яндекс Станции 2 (прошивка 1.56, HAR).

Клиент -> сервер:  {"event": {"header": {namespace, name, messageId[, streamId]}, "payload": {...}}}
Сервер -> клиент:  {"directive": {"header": {namespace, name, messageId, refMessageId[, streamId]}, "payload": {...}}}
Управление потоками: {"streamcontrol": {"action": 0, "reason": 0, "streamId": N, "messageId": ...}}
Бинарные фреймы: 4 байта streamId (big-endian) + Ogg/Opus. Клиентские потоки нечётные, серверные — чётные.
"""
from __future__ import annotations

import time
import uuid


def mid() -> str:
    """messageId в стиле uniproxy: 4 группы hex через дефис."""
    h = uuid.uuid4().hex
    return f"{h[0:8]}-{h[8:16]}-{h[16:24]}-{h[24:32]}"


def directive(namespace: str, name: str, payload: dict, ref: str | None = None, stream_id: int | None = None,
              **header_extra) -> dict:
    header: dict = {"namespace": namespace, "name": name, "messageId": mid()}
    if ref is not None:
        header["refMessageId"] = ref
    if stream_id is not None:
        header["streamId"] = stream_id
    header.update(header_extra)
    return {"directive": {"header": header, "payload": payload}}


def stream_control(stream_id: int, action: int = 0, reason: int = 0) -> dict:
    # action 0 — закрыть поток; reason 0 — штатно
    return {"streamcontrol": {"messageId": mid(), "reason": reason, "action": action, "streamId": stream_id}}


def sync_state_response(ref: str, session_id: str, connection_data: dict | None = None,
                        keep_message_id: bool = False) -> dict:
    payload: dict = {"SessionId": session_id}
    if connection_data:
        payload["connection_data"] = connection_data
    d = directive("System", "SynchronizeStateResponse", payload, ref=ref)
    if not keep_message_id:
        d["directive"]["header"].pop("messageId")  # в трафике 1.56 messageId здесь нет
    return d


def push_notify(ref: str) -> dict:
    ack = int(time.time() * 1000)
    return directive(
        "System", "Push",
        {
            "analytics_meta_info": {"request_id": f"{ack * 1000}${ref}${mid()}"},
            "directives": [{"name": "notify", "payload": {"version_id": "0"}}],
        },
        ack=ack,
    )


def ping(timestamp_s: int | None = None, ping_timeout_ms: int = 10000,
         pong_timeout_ms: int = 10000, ref: str | None = None) -> dict:
    """System.Ping — серверный keep-alive.

    Для прошивок 3.x в aliceapi превращается в `TServerMessage.Event.Ping`
    (`TPing`). В протоколе `Timestamp` — **uint32**, поэтому здесь секунды
    эпохи (миллисекунды в него не влезают), а таймауты — миллисекунды.

    ``ref`` заполняется, когда Ping отправляется как ОТВЕТ («понг») на Ping
    станции: отдельного ``TPong`` в AAPI нет, «понгом» служит Ping с тем же
    ``Timestamp`` и ``refMessageId`` = messageId входящего Ping.
    """
    ts = int(time.time()) if timestamp_s is None else int(timestamp_s)
    return directive("System", "Ping", {
        "timestamp": ts & 0xFFFFFFFF,
        "ping_timeout_ms": int(ping_timeout_ms),
        "pong_timeout_ms": int(pong_timeout_ms),
    }, ref=ref)


def push_empty() -> dict:
    """System.Push без директив — пустой, но валидный пуш (keep-alive).

    ``Push`` есть в ``SupportedServerEvents`` станции, поэтому она его принимает
    и отвечает ``System.Ack``; по этому Ack видно, что соединение живое.
    """
    ack = int(time.time() * 1000)
    return directive("System", "Push",
                     {"analytics_meta_info": {"request_id": f"{ack * 1000}${mid()}"}, "directives": []},
                     ack=ack)


def input_start_ack(ref: str) -> dict:
    return directive("System", "InputStartAck",
                     {"input_start_ack": {"request_start_time": int(time.time() * 1_000_000)}}, ref=ref)


def asr_result(ref: str, text: str, partial_number: int, end_of_utt: bool, duration_ms: int) -> dict:
    words = [{"value": w, "confidence": 1} for w in text.split()]
    recognition = [{"normalized": text, "words": words, "confidence": -0.5 if text else -0.001, "parentModel": ""}]
    payload = {
        "recognition": recognition,
        "messagesCount": 1,
        "metainfo": {},
        "responseCode": "OK",
        "doNotSendToClient": False,
        "asr_partial_number": partial_number,
        "endOfUtt": end_of_utt,
        "bioResult": [],
        "contextRef": [],
        "durationProcessedAudio": str(duration_ms),
        "whisperInfo": {"isWhisper": False},
    }
    if end_of_utt:
        payload["metainfo"] = {"version": "aqw", "topic": "gigaam", "serverHostname": "local",
                               "serverVersion": "1"}
    return directive("ASR", "Result", payload, ref=ref, eventId=ref)


def spotter_validation(ref: str, device_id: str, valid: bool = True) -> dict:
    return directive("Spotter", "Validation", {
        "allow_activation_by_unvalidated_spotter": 0,
        "multiactivation_id": f"{int(time.time() * 1000)}{device_id}",
        "valid": int(valid),
        "result": int(valid),
        "canceled_cause_of_multiactivation": 0,
    }, ref=ref)


def vins_response(ref: str, request_id: str, session_id: str, sequence_number: int, text: str,
                  speech: str | None = None, directives: list[dict] | None = None,
                  should_listen: bool = False) -> dict:
    """Vins.VinsResponse. text — карточка (отображение), speech — что озвучиваем."""
    has_voice = bool(speech if speech is not None else text)
    response: dict = {
        "experiments": {},
        "directives_execution_policy": "BeforeSpeech",
        "directives": list(directives or []),
    }
    if text:
        card_id = mid()
        response["card"] = {"card_id": card_id, "type": "simple_text", "text": text}
        response["cards"] = [{"card_id": card_id, "type": "simple_text", "text": text}]
    if has_voice:
        # отмечает в последовательности директив место, где играет TTS
        response["directives"].insert(0, {"name": "tts_play_placeholder", "payload": {"channel": "Dialog"},
                                          "sub_name": "tts_play_placeholder", "type": "client_action",
                                          "is_led_silent": True})
    voice_response: dict = {
        "has_voice_response": has_voice,
        "tts_voice_settings": {"effective_language": "L_RUS", "main_request_language": "L_RUS"},
        "should_listen": should_listen,
    }
    if has_voice:
        voice_response["output_speech"] = {"type": "simple", "text": speech if speech is not None else text}
    payload = {
        "version": "aqw",
        "header": {
            "response_id": mid(),
            "ref_message_id": ref,
            "request_id": request_id,
            "sequence_number": sequence_number,
            "parent_request_id": request_id,
            "session_id": session_id,
            "dialog_id": None,
        },
        "response": response,
        "voice_response": voice_response,
    }
    return directive("Vins", "VinsResponse", payload, ref=ref)


def tts_speak(ref: str, stream_id: int) -> dict:
    return directive("TTS", "Speak", {
        "from_cache": False,
        "enable_bargin": False,
        "lazy_tts_streaming": False,
        "disableInterruptionSpotter": False,
        "format": "audio/opus",
    }, ref=ref, stream_id=stream_id)


def client_action(name: str, payload: dict | None = None, sub_name: str | None = None) -> dict:
    d = {"name": name, "payload": payload or {}, "type": "client_action"}
    if sub_name:
        d["sub_name"] = sub_name
    return d
