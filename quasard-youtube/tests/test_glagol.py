from __future__ import annotations

import asyncio

from quasard_youtube.config import Config
from quasard_youtube.glagol import GlagolClient


def test_state_and_response_are_parsed() -> None:
    async def run() -> None:
        client = GlagolClient(Config())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        client._pending["req-1"] = future
        client._handle_message(
            {
                "requestId": "req-1",
                "status": "SUCCESS",
                "supported_features": ["audio_client", "multiroom"],
                "state": {
                    "playing": True,
                    "volume": 0.42,
                    "playerState": {"playerType": "radio"},
                },
            }
        )
        assert await future == {"status": "SUCCESS"}
        assert client.state.audio_client is True
        assert client.state.playing is True
        assert client.state.volume == 0.42
        assert client.state.player_type == "radio"

    asyncio.run(run())
