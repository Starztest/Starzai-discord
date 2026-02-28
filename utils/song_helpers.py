"""
Shared song helpers used by both music.py and music_premium.py.

Centralises the ``song_key`` function so the same serialisation logic
is never duplicated across cogs, and provides a reusable temp-file
context manager for safe cleanup of downloaded audio.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


def song_key(song: Dict[str, Any]) -> str:
    """Create a stable JSON key for storing a song in the database.

    Stores only the fields needed for retrieval/display so that
    different API responses for the same song produce the same key.
    """
    return json.dumps(
        {
            "id": song.get("id", ""),
            "name": song.get("name", ""),
            "artist": song.get("artist", ""),
            "album": song.get("album", ""),
            "year": song.get("year", ""),
            "duration": song.get("duration", 0),
            "duration_formatted": song.get("duration_formatted", "0:00"),
            "image": song.get("image", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@contextmanager
def temp_audio_file(suffix: str = ".mp3") -> Iterator[str]:
    """Context manager that yields a temp file path and cleans up on exit.

    Usage::

        with temp_audio_file() as path:
            # download / write audio to *path*
            ...
        # file is deleted automatically when the block exits
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
