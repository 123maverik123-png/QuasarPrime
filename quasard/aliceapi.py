"""Бинарный protobuf-протокол uniproxy («proto Alice API») — им говорят прошивки 3.x.

Кадр: бинарный WS-фрейм "AAPI" + сериализованный NAlice.NAliceApi.TClientMessage / TServerMessage.
Аудио по-прежнему идёт кадрами [streamId BE32][данные].

Схемы (aliceapi.desc) восстановлены из встроенных дескрипторов libquasar_daemons.so прошивки
3.367.1.28. Сервер внутри работает с JSON-сообщениями (protocol.py), а здесь они переводятся в proto
и обратно — так один конвейер обслуживает обе версии прошивки.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

MAGIC = b"AAPI"

# streamcontrol.reason JSON <-> EReason proto
_REASON_TO_JSON = {"OK": 0, "INTERNAL_ERROR": 2, "STOP_RECORDING": 5, "AUDIO_SOURCE_STOPPED": 6, "CANCEL": 7,
                   "NO_SPEECH": 8}
_JSON_TO_REASON = {v: k for k, v in _REASON_TO_JSON.items()}


@lru_cache(maxsize=1)
def _pool() -> descriptor_pool.DescriptorPool:
    fds = descriptor_pb2.FileDescriptorSet.FromString((Path(__file__).parent / "aliceapi.desc").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for f in fds.file:
        pool.Add(f)
    return pool


@lru_cache(maxsize=1)
def _classes():
    pool = _pool()
    get = lambda n: message_factory.GetMessageClass(pool.FindMessageTypeByName(n))  # noqa: E731
    return get("NAlice.NAliceApi.TServerMessage"), get("NAlice.NAliceApi.TClientMessage")


@lru_cache(maxsize=1)
def _semantic_cls():
    return message_factory.GetMessageClass(
        _pool().FindMessageTypeByName("NAlice.NAliceApi.TSemanticFrameRequestData")
    )


def decode_semantic_frame(raw: str | bytes) -> dict:
    """PayloadRaw события server_action -> читаемый семантический кадр.

    Станция присылает ``server_action`` с пустым ``TimedPayload``: имя кадра и
    интент лежат в сырых байтах (``Event.PayloadRaw``), которые разбираются как
    ``TSemanticFrameRequestData``::

        TypedSemanticFrame.PlayerContinueSemanticFrame
        Analytics.Purpose = "player_continue", Origin = "SmartSpeaker"

    Возвращает ``{"typed": [имена кадров], "purpose": ..., "origin": ...}``;
    на битых данных — пустой словарь.
    """
    if not raw:
        return {}
    try:
        data = raw if isinstance(raw, bytes) else base64.b64decode(raw)
        msg = _semantic_cls().FromString(data)
        obj = json_format.MessageToDict(msg, preserving_proto_field_name=True)
    except Exception:  # noqa: BLE001 - диагностика не должна ронять обработку
        return {}
    typed = obj.get("TypedSemanticFrame") or {}
    analytics = obj.get("Analytics") or {}
    return {
        "typed": [k for k in typed if k != "Analytics"],
        "purpose": analytics.get("Purpose") or "",
        "origin": analytics.get("Origin") or "",
    }


def decode_client(frame: bytes) -> dict:
    _, client_cls = _classes()
    msg = client_cls.FromString(frame[len(MAGIC):])
    return json_format.MessageToDict(msg, preserving_proto_field_name=True)


def encode_server(msg: dict) -> bytes:
    server_cls, _ = _classes()
    return MAGIC + json_format.ParseDict(msg, server_cls(), ignore_unknown_fields=True).SerializeToString()


# для имитатора станции (tools/station_sim.py --proto)
def encode_client(msg: dict) -> bytes:
    _, client_cls = _classes()
    return MAGIC + json_format.ParseDict(msg, client_cls(), ignore_unknown_fields=True).SerializeToString()


def decode_server(frame: bytes) -> dict:
    server_cls, _ = _classes()
    return json_format.MessageToDict(server_cls.FromString(frame[len(MAGIC):]), preserving_proto_field_name=True)


# ------------------------------------------------------------------ proto -> JSON (входящие)

def client_to_json(m: dict) -> dict | None:
    """TClientMessage (dict) -> JSON-сообщение в формате прошивки 1.56 (event / streamcontrol)."""
    if "StreamControl" in m:
        sc = m["StreamControl"]
        if "SpotterEnd" in sc:
            action, reason = 2, 0
        elif "Close" in sc:
            action, reason = 0, _REASON_TO_JSON.get(sc["Close"].get("Reason", "OK"), 0)
        else:
            return None  # LazyTTS — не используем
        return {"streamcontrol": {"action": action, "reason": reason, "streamId": int(sc.get("StreamId", 0)),
                                  "messageId": sc.get("MessageId", "")}}
    ev = m.get("Event")
    if not ev:
        return None
    h = ev.get("Header", {})
    header = {"messageId": h.get("MessageId", ""), "refMessageId": h.get("RefMessageId", "")}
    if "StreamId" in h:
        header["streamId"] = int(h["StreamId"])
    kind = next((k for k in ev if k != "Header"), None)
    body = ev.get(kind) or {}
    names = {
        "SynchronizeStateRequest": ("System", "SynchronizeState"),
        "VoiceInput": ("Vins", "VoiceInput"),
        "TextInput": ("Vins", "TextInput"),
        "LogSpotter": ("Log", "Spotter"),
        "RequestStat": ("Log", "RequestStat"),
        "Ack": ("System", "Ack"),
        "Ping": ("System", "Ping"),
        "CancelRequest": ("Vins", "CancelRequest"),
    }
    ns, name = names.get(kind, ("Proto", kind or "Unknown"))
    payload: dict = {"proto": body}
    if kind in ("VoiceInput", "TextInput"):
        rh = body.get("Header", {})
        event = (body.get("Request") or {}).get("Event") or {}
        payload = {
            "header": {"request_id": rh.get("RequestId"), "sequence_number": rh.get("SequenceNumber", 0)},
            "enable_spotter_validation": bool(body.get("EnableSpotterValidation")),
            "request": {"event": {"type": event.get("Type"), "text": event.get("Text"), "name": event.get("Name"),
                                  "payload": event.get("Payload") or {},
                                  # PayloadRaw — сырые байты payload (proto.Struct «теряет» поля,
                                  # которых нет в схеме; для @@mm_semantic_frame данные обычно тут)
                                  "payload_raw": event.get("PayloadRaw") or ""}},
        }
        if kind == "TextInput" and event.get("Type") in (None, "text_input"):
            payload["request"]["event"]["type"] = "text_input"
    return {"event": {"header": {"namespace": ns, "name": name, **header}, "payload": payload}}


# ------------------------------------------------------------------ JSON -> proto (исходящие)

def _directive(d: dict) -> dict:
    out = {"Type": d.get("type", "client_action"), "Name": d.get("name", ""), "AnalyticsType": d.get("sub_name") or d.get("name", ""),
           "Payload": d.get("payload") or {}}
    if d.get("is_led_silent"):
        out["IsLedSilent"] = True
    return out


def server_to_proto(msg: dict) -> dict | None:
    """JSON-сообщение сервера -> TServerMessage (dict). None — у этого сообщения нет proto-аналога."""
    if "streamcontrol" in msg:
        sc = msg["streamcontrol"]
        return {"StreamControl": {"StreamId": sc["streamId"], "MessageId": sc.get("messageId", ""),
                                  "Close": {"Reason": _JSON_TO_REASON.get(sc.get("reason", 0), "OK")}}}
    d = msg["directive"]
    h, p = d["header"], d["payload"]
    header = {"MessageId": h.get("messageId", ""), "RefMessageId": h.get("refMessageId", "")}
    if "streamId" in h:
        header["StreamId"] = h["streamId"]
    name = f"{h['namespace']}.{h['name']}"
    if name == "System.SynchronizeStateResponse":
        body = {"SynchronizeStateResponse": {"SessionId": p.get("SessionId", "")}}
        if p.get("connection_data"):
            body["SynchronizeStateResponse"]["ConnectionData"] = p["connection_data"]
    elif name == "System.Push":
        header["Ack"] = h.get("ack", 0)
        body = {"Push": {"Directives": [_directive(x) for x in p.get("directives", [])]}}
    elif name == "System.Ping":
        # TPing: все три поля uint32 — значение из JSON всё равно усекаем
        body = {"Ping": {"Timestamp": int(p.get("timestamp") or 0) & 0xFFFFFFFF,
                         "PingTimeoutMilliSeconds": int(p.get("ping_timeout_ms") or 0) & 0xFFFFFFFF,
                         "PongTimeoutMilliSeconds": int(p.get("pong_timeout_ms") or 0) & 0xFFFFFFFF}}
    elif name == "System.InputStartAck":
        body = {"InputStartAck": {"RequestStartTime": p["input_start_ack"]["request_start_time"]}}
    elif name == "ASR.Result":
        rec = [{"Normalized": r["normalized"], "Confidence": 0.999,
                "Words": [{"Value": w["value"], "Confidence": 0.999} for w in r["words"]]}
               for r in p["recognition"] if r.get("normalized")]
        body = {"AsrResult": {"EndOfUtt": p["endOfUtt"], "MessagesCount": 1, "Recognition": rec}}
    elif name == "Spotter.Validation":
        body = {"SpotterValidation": {"Result": "Ok" if p.get("valid") else "SpottingRejected"}}
    elif name == "Log.Ack":
        body = {"LogAck": {}}
    elif name == "Log.RequestStatAck":
        body = {"RequestStatAck": {}}
    elif name == "TTS.Speak":
        body = {"TtsSpeak": {"Format": p.get("format", "audio/opus"), "LazyTtsStreaming": False}}
    elif name == "Vins.VinsResponse":
        r, vr, ph = p.get("response", {}), p.get("voice_response", {}), p.get("header", {})
        voice = {"HasVoiceResponse": bool(vr.get("has_voice_response")), "ShouldListen": bool(vr.get("should_listen"))}
        if vr.get("output_speech"):
            voice["OutputSpeech"] = {"Text": vr["output_speech"].get("text", "")}
        body = {"AliceResponse": {
            "Header": {"RequestId": ph.get("request_id") or "", "ResponseId": ph.get("response_id", ""),
                       "SequenceNumber": ph.get("sequence_number") or 0, "ParentRequestId": ph.get("request_id") or ""},
            "Response": {"Cards": [{"Type": c.get("type", "simple_text"), "Text": c.get("text", ""),
                                    "CardId": c.get("card_id", "")} for c in r.get("cards", [])],
                         "Directives": [_directive(x) for x in r.get("directives", [])]},
            "VoiceResponse": voice,
        }}
    else:
        return None
    return {"Event": {"Header": header, **body}}
