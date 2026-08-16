"""Async client library for the Yamaha Disklavier ENSPIRE local API.

Two ways in, and you need both: :class:`Disklavier` drives the piano over HTTP, and
:class:`DisklavierShare` reaches its SMB share, which is the only route for putting your own
MIDI on the instrument.
"""

from __future__ import annotations

from .client import Disklavier
from .const import (
    AUDIO_SUFFIXES,
    DEFAULT_EXCLUDES,
    DEFAULT_PORT,
    INDEXED_DEPTH_LIMIT,
    PLAYABLE_SUFFIXES,
    SHARE_ENSPIRE_CONTROLLER,
    SHARE_PC_SHARING,
    SMB_PORT,
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
    SearchKind,
    SongFormat,
    SongGroup,
)
from .exceptions import (
    DisklavierCommandError,
    DisklavierConnectionError,
    DisklavierError,
    DisklavierResponseError,
    DisklavierShareAuthError,
    DisklavierShareError,
    DisklavierShareExistsError,
    DisklavierShareNotFoundError,
)
from .models import (
    Album,
    CurrentInfo,
    LibrarySong,
    MasterState,
    PlaybackSnapshot,
    Playlist,
    RadioChannel,
    SearchResult,
    Song,
    SongDatabase,
    StaticInfo,
)
from .share import (
    DisklavierShare,
    ShareEntry,
    SyncAction,
    SyncFailure,
    SyncProgress,
    SyncResult,
)

__version__ = "0.2.1"

__all__ = [
    "AUDIO_SUFFIXES",
    "DEFAULT_EXCLUDES",
    "DEFAULT_PORT",
    "INDEXED_DEPTH_LIMIT",
    "PLAYABLE_SUFFIXES",
    "SHARE_ENSPIRE_CONTROLLER",
    "SHARE_PC_SHARING",
    "SMB_PORT",
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
    "DisklavierShare",
    "DisklavierShareAuthError",
    "DisklavierShareError",
    "DisklavierShareExistsError",
    "DisklavierShareNotFoundError",
    "Genre",
    "GenreSelect",
    "LibrarySong",
    "MasterState",
    "PlaybackSnapshot",
    "PlaybackStatus",
    "Playlist",
    "PlaylistGroup",
    "PowerStatus",
    "QuietMode",
    "RadioChannel",
    "RepeatMode",
    "SearchKind",
    "SearchResult",
    "ShareEntry",
    "Song",
    "SongDatabase",
    "SongFormat",
    "SongGroup",
    "StaticInfo",
    "SyncAction",
    "SyncFailure",
    "SyncProgress",
    "SyncResult",
    "__version__",
]
