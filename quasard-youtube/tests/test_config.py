from __future__ import annotations

from pathlib import Path

import pytest

from quasard_youtube.config import ConfigError, load_config

_TMP = Path(__file__).with_name("_tmp_config.toml")


@pytest.fixture
def cfg_file():
    _TMP.unlink(missing_ok=True)
    yield _TMP
    _TMP.unlink(missing_ok=True)


def test_env_and_toml(cfg_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_SESSION_ID", "secret-cookie")
    cfg_file.write_text(
        """
[station]
host = "192.168.50.104"
session_id = "${YANDEX_SESSION_ID}"

[server]
advertise_host = "192.168.50.95"

[audio]
bitrate_kbps = 192
""",
        encoding="utf-8",
    )
    config = load_config(cfg_file)
    assert config.station.session_id == "secret-cookie"
    assert config.session_id == "secret-cookie"
    assert config.audio.bytes_per_second == 24000
    assert config.stream_url("abc") == "http://192.168.50.95:8099/stream/abc.mp3"
    public = config.public_dict()
    assert public["station"]["session_id"] == "***"


def test_missing_file() -> None:
    with pytest.raises(ConfigError):
        load_config(Path(__file__).with_name("absent.toml"))


def test_unknown_key(cfg_file: Path) -> None:
    cfg_file.write_text("[station]\nhost='x'\nunknown=1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Неизвестные ключи"):
        load_config(cfg_file)


def test_session_prefix_is_normalized(cfg_file: Path) -> None:
    cfg_file.write_text(
        "[station]\nhost='192.168.1.2'\nsession_id='Session_id=abc'\n",
        encoding="utf-8",
    )
    assert load_config(cfg_file).session_id == "abc"
