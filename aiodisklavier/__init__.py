"""Async client library for the Yamaha Disklavier ENSPIRE local HTTP API."""

from __future__ import annotations

from .client import Disklavier
from .const import (
    DEFAULT_PORT,
    UPNP_DEVICE_TYPE,
    VOLUME_MAX,
    VOLUME_MIN,
    VOLUME_STEP,
    Genre,
    GenreSelect,
    PlaybackStatus,
    PlaylistGroup,
    PowerStatus,
    QuietMode,
    RepeatMode,
    SongGroup,
)
from .exceptions import (
    DisklavierCommandError,
    DisklavierConnectionError,
    DisklavierError,
    DisklavierResponseError,
)
from .models import (
    Album,
    CurrentInfo,
    MasterState,
    PlaybackSnapshot,
    Playlist,
    RadioChannel,
    Song,
    StaticInfo,
)

__version__ = "0.1.1"

__all__ = [
    "DEFAULT_PORT",
    "UPNP_DEVICE_TYPE",
    "VOLUME_MAX",
    "VOLUME_MIN",
    "VOLUME_STEP",
    "Album",
    "CurrentInfo",
    "Disklavier",
    "DisklavierCommandError",
    "DisklavierConnectionError",
    "DisklavierError",
    "DisklavierResponseError",
    "Genre",
    "GenreSelect",
    "MasterState",
    "PlaybackSnapshot",
    "PlaybackStatus",
    "Playlist",
    "PlaylistGroup",
    "PowerStatus",
    "QuietMode",
    "RadioChannel",
    "RepeatMode",
    "Song",
    "SongGroup",
    "StaticInfo",
    "__version__",
]
