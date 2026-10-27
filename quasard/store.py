"""Данные станций: захваченные из HAR ответы (data/captured) и состояние устройств (data/devices)."""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("store")


class Store:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.captured = cfg.data_path / "captured"
        self.devices = cfg.data_path / "devices"
        self.devices.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- captured
    def captured_json(self, name: str):
        p = self.captured / f"{name}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    # ---------------------------------------------------------------- devices
    def _device_file(self, device_id: str) -> Path:
        safe = "".join(c for c in device_id if c.isalnum() or c in "-_") or "unknown"
        return self.devices / f"{safe}.json"

    def device(self, device_id: str) -> dict:
        p = self._device_file(device_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def save_device(self, device_id: str, data: dict) -> None:
        self.devices.mkdir(parents=True, exist_ok=True)  # папку могли удалить при работающем сервере
        self._device_file(device_id).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def update_device(self, device_id: str, **fields) -> dict:
        d = self.device(device_id)
        d.update(fields)
        d.setdefault("id", device_id)
        self.save_device(device_id, d)
        return d

    def known_devices(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.devices.glob("*.json"))]

    def glagol_security(self, device_id: str) -> dict:
        """Сертификат локального glagol-сервера станции (стабильный, хранится на диске)."""
        d = self.device(device_id)
        if "glagol" not in d:
            d["glagol"] = {"security": _self_signed_localhost()}
            d.setdefault("id", device_id)
            self.save_device(device_id, d)
        return d["glagol"]

    # ---------------------------------------------------------------- ответы quasar
    def sync_info(self, device_id: str, platform: str) -> dict:
        base = self.captured_json("get_sync_info")
        if base is None:
            base = self._default_sync_info()
        info = copy.deepcopy(base)
        info["glagol"] = self.glagol_security(device_id)
        info["status"] = "ok"
        cfg = info.setdefault("config", {})
        sysc = cfg.setdefault("system_config", {})
        if self.cfg.server.public_uniproxy_url:
            sysc["uniProxyUrl"] = self.cfg.server.public_uniproxy_url
        # Наш uniproxy говорит JSON-протоколом — бинарный protobuf (AAPI) не включаем
        vds = sysc.setdefault("voiceDialogSettings", {})
        vds["useProtoAliceApi"] = False
        vds.pop("protoAliceApi", None)
        for k in ("sendVinsRequestProto", "useVinsRequestProto"):
            cfg.pop(k, None)
        # Не даём станции обновиться (новая прошивка может сломать совместимость)
        upd = sysc.get("com.yandex.launcher.updaterapp", {}).get("settings", {})
        for k in ("allowFirmwareUpdate", "allowFirmwareUpdateV2"):
            if k in upd:
                upd[k] = {"ota": False}
        dev = self.device(device_id)
        if dev.get("name"):
            cfg.setdefault("device_config", {})["name"] = dev["name"]
        return info

    def _default_sync_info(self) -> dict:
        d = self.cfg.device
        return {
            "alice_pro_subscription": {"enabled": False},
            "config": {
                "account_config": {
                    "spotter": "alisa", "jingle": False, "smartActivation": True,
                    "contentAccess": "without", "childContentAccess": "children",
                    "aliceProactivity": False, "saveHistoryUsage": False,
                },
                "device_config": {
                    "locale": "ru-RU", "name": d.name, "beta": False, "tv_beta": False,
                    "location": {"latitude": d.latitude, "longitude": d.longitude},
                    "dndMode": {"enabled": False, "features": {"allowIncomingCalls": False}},
                    "standby": {"deepStandbyEnabled": True, "deepStandbyTimeoutMinutes": 240},
                },
                "system_config": {
                    "uniProxyUrl": "wss://uniproxy.alice.yandex.net/uni.ws",
                    "voiceDialogSettings": {"useProtoAliceApi": False},
                    "onlineSpotterEnabled": False,
                },
            },
            "subscription": {"mode": "transaction"},
            "tags": [],
            "status": "ok",
        }

    def device_list(self, device_id: str) -> dict:
        base = self.captured_json("device_list")
        devices = copy.deepcopy(base["devices"]) if base else []
        ids = {x.get("id") for x in devices}
        for dev in self.known_devices():
            if dev.get("id") and dev["id"] not in ids and dev.get("platform"):
                devices.append(self._device_entry(dev["id"], dev))
                ids.add(dev["id"])
        if device_id and device_id not in ids:
            devices.append(self._device_entry(device_id, self.device(device_id)))
        for x in devices:
            if x.get("id") and "glagol" in x:
                x["glagol"] = self.glagol_security(x["id"])
        return {"devices": devices, "status": "ok"}

    def _device_entry(self, device_id: str, dev: dict) -> dict:
        d = self.cfg.device
        name = dev.get("name") or d.name
        return {
            "activation_code": 0,
            "activation_region": "RU",
            "config": {
                "locale": "ru-RU", "name": name,
                "location": {"latitude": d.latitude, "longitude": d.longitude},
                "dndMode": {"enabled": False, "features": {"allowIncomingCalls": False}},
                "standby": {"deepStandbyEnabled": True, "deepStandbyTimeoutMinutes": 240},
            },
            "glagol": self.glagol_security(device_id),
            "id": device_id,
            "name": name,
            "networkInfo": dev.get("networkInfo", {}),
            "platform": dev.get("platform", "yandexstation_2"),
            "promocode_activated": False,
            "tags": [],
        }


def _self_signed_localhost() -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Yandex"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
    return {
        "server_certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "server_private_key": key.private_bytes(serialization.Encoding.PEM,
                                                serialization.PrivateFormat.TraditionalOpenSSL,
                                                serialization.NoEncryption()).decode(),
    }
