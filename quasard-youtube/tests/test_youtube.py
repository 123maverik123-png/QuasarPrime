from __future__ import annotations

import pytest

from quasard_youtube.youtube import ResolveError, track_from_entry


def test_flat_search_entry() -> None:
    entry = {
        "_type": "url",
        "id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up",
        "duration": 214,
        "uploader": "Rick Astley",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    info = track_from_entry(entry, "rick")
    assert info.video_id == "dQw4w9WgXcQ"
    assert info.duration == 214
    assert info.artist == "Rick Astley"
    assert info.webpage_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert info.display_title.startswith("Rick Astley — ")


def test_live_is_rejected() -> None:
    with pytest.raises(ResolveError, match="трансляции"):
        track_from_entry(
            {
                "id": "x",
                "title": "live",
                "duration": 100,
                "url": "https://youtube.com/watch?v=x",
                "is_live": True,
            }
        )


def test_missing_duration_is_rejected() -> None:
    with pytest.raises(ResolveError, match="длительность"):
        track_from_entry(
            {
                "id": "x",
                "title": "t",
                "url": "https://youtube.com/watch?v=x",
            }
        )
