from __future__ import annotations

from quasard_youtube.audio import ffmpeg_args
from quasard_youtube.config import Config


def test_ffmpeg_is_cbr_mp3_and_has_no_xing() -> None:
    args = ffmpeg_args(Config())
    joined = " ".join(args)
    assert "-b:a 192k" in joined
    assert "-minrate 192k" in joined
    assert "-maxrate 192k" in joined
    assert "-write_xing 0" in joined
    assert "-f mp3 pipe:1" in joined
    assert "-ac 2" in joined
    assert "-ar 44100" in joined


def test_no_volume_filter_by_default() -> None:
    assert "-af" not in ffmpeg_args(Config())


def test_volume_filter_is_inserted_before_encoder() -> None:
    config = Config()
    config.audio.filter = "volume=-4.5dB"
    args = ffmpeg_args(config)
    assert args[args.index("-af") + 1] == "volume=-4.5dB"
    # -af — выходная опция: должна идти до кодера
    assert args.index("-af") < args.index("-c:a")


def test_blank_filter_is_ignored() -> None:
    config = Config()
    config.audio.filter = "   "
    assert "-af" not in ffmpeg_args(config)
