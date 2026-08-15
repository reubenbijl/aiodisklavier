"""Constants and enumerations for the Disklavier ENSPIRE local API.

Values here mirror the firmware exactly. See ``docs/enspire-api.md`` for provenance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# Open API. The piano serves this two ways: a version-namespaced path,
# ``/api/1.0/<command>?<params>``, and the CGI script behind it,
# ``/api/api.php?_com=<command>&<params>``. They are equivalent -- same bodies, same 400s,
# same valueless-flag handling, all verified on 5.24.00. The versioned path is what Yamaha's
# own on-device test harness (``/ctrl/api_test.html``) calls, and it matches the
# ``api_version`` field static_info reports, so it is treated here as the public contract and
# api.php as an implementation detail.
API_VERSION: Final = "1.0"
PATH_API_BASE: Final = f"/api/{API_VERSION}"
PATH_STATIC_INFO: Final = f"{PATH_API_BASE}/static_info"
PATH_CURRENT_INFO: Final = f"{PATH_API_BASE}/current_info"

# Internal endpoints, used only for what the open API does not cover.
PATH_CTRL_SEQ: Final = "/ctrl/setSeq.php"
PATH_CTRL_SONG: Final = "/ctrl/setSong.php"
PATH_CTRL_MASTER_JSON: Final = "/ctrl/master.json"
PATH_CTRL_PUT_NOTE_ON: Final = "/ctrl/putNoteOn.php"
PATH_CTRL_REFRESH_DB: Final = "/ctrl/setRefreshDB.php"

DEFAULT_PORT: Final = 80
DEFAULT_TIMEOUT: Final = 10.0

#: Upper bound on a response body, in bytes. The largest payload the piano actually serves
#: is a full song list at a few hundred kB, so anything past this is not the piano talking.
#: Reads stop here rather than letting a hostile or broken device stream without limit.
MAX_RESPONSE_BYTES: Final = 1024 * 1024

# The state endpoints are files the control daemon rewrites in place, so a read can catch
# one mid-write and come back truncated. Observed on 5.24.00 against /api/current_info.
# These reads are idempotent, so a short retry is safe and cheaper than surfacing a fault.
#: How many times to re-read a state endpoint that returned malformed JSON. Truncation was
#: only ever observed while a song was actively playing -- 160 reads on an idle piano were
#: all clean -- so the budget is sized to outlast a rewrite rather than to be frugal.
JSON_RETRY_ATTEMPTS: Final = 4
#: How long to wait before re-reading, in seconds. Four attempts spans roughly a second,
#: comfortably longer than the daemon's own 500 ms refresh.
JSON_RETRY_DELAY: Final = 0.25

# Defaults for :meth:`aiodisklavier.Disklavier.async_notify`.
#: How long to wait for a one-shot notification to finish before restoring anyway.
NOTIFY_WAIT_TIMEOUT: Final = 30.0
#: How often to poll while waiting for the notification to finish.
NOTIFY_POLL_INTERVAL: Final = 0.5
#: A short grace period after issuing the notification, before polling for its end. The
#: firmware takes a moment to flip ``playback_status`` to ``play``; without this the poll
#: would see the pre-play state and return immediately.
NOTIFY_SETTLE: Final = 1.0

#: UPnP device type advertised over SSDP, used for discovery.
UPNP_DEVICE_TYPE: Final = "urn:schemas-upnp-org:device:Disklavier:1"

#: ``volume_up_main`` / ``volume_down_main`` move by this much. Confirmed on 5.24.00.
VOLUME_STEP: Final = 10

VOLUME_MIN: Final = 0
VOLUME_MAX: Final = 100


class PowerStatus(StrEnum):
    """Values reported by ``power_status``.

    ``WAKEUP`` is transitional: ``SLEEP`` -> ``WAKEUP`` -> ``ON`` takes roughly 12 seconds.
    The HTTP API stays responsive throughout, including while asleep, so reachability says
    nothing about power state.
    """

    ON = "on"
    SLEEP = "sleep"
    WAKEUP = "wakeup"


class PlaybackStatus(StrEnum):
    """Values reported by ``playback_status``.

    There is deliberately no ``STOP``. The ``stop`` command yields ``PAUSE`` with a position
    of zero -- see :meth:`aiodisklavier.Disklavier.async_stop`.
    """

    PLAY = "play"
    PAUSE = "pause"


class QuietMode(StrEnum):
    """Whether the hammers physically strike the strings."""

    ACOUSTIC = "acoustic"
    QUIET = "quiet"


class SongGroup(StrEnum):
    """Song libraries, as accepted by the open API's ``group`` parameter."""

    BUILT_IN_SONGS = "built_in_songs"
    BUILT_IN_PLAYLIST = "built_in_playlist"
    MY_SONGS = "my_songs"
    MY_RECORDINGS = "my_recordings"
    PC_SHARING_FOLDER = "pc_sharing_folder"
    DOWNLOADED_SONGS = "downloaded_songs"


class PlaylistGroup(StrEnum):
    """Playlist libraries, as accepted by the open API's ``group`` parameter."""

    DEMO_PLAYLIST = "demo_playlist"
    PLAYLISTS = "playlists"


class Genre(StrEnum):
    """Genre folders within :attr:`SongGroup.BUILT_IN_SONGS`."""

    POP = "pop"
    ROCK = "rock"
    JAZZ = "jazz"
    RNB_SOUL = "rnb_soul"
    CLASSICAL = "classical"
    COUNTRY = "country"
    HOLIDAYS = "holidays"
    SOUNDTRACK = "soundtrack"
    PIANO50 = "piano50"
    LESSON = "lesson"
    SMARTKEY = "smartkey"


class GenreSelect(StrEnum):
    """How to pick a song within a :class:`Genre`."""

    TOP = "top"
    RANDOM = "random"


#: ``master.json`` reports the current library as a bare one-letter prefix, while the open API
#: takes the long-form group name. This maps back, so a snapshot taken from the internal
#: endpoint can be restored through the open API.
PREFIX_TO_SONG_GROUP: Final[dict[str, SongGroup]] = {
    "d": SongGroup.BUILT_IN_SONGS,
    "l": SongGroup.BUILT_IN_PLAYLIST,
    # "s" is inferred, not observed: the my_songs library was empty on the reference
    # unit, so its prefix never appeared in master.json. If my_songs actually uses a
    # different prefix, that prefix is unrecognised and restore is skipped; if "s"
    # turns out to belong to some other library, restore would reselect into my_songs
    # -- drop this entry if that is ever observed.
    "s": SongGroup.MY_SONGS,
    "r": SongGroup.MY_RECORDINGS,
    "f": SongGroup.PC_SHARING_FOLDER,
    "y": SongGroup.DOWNLOADED_SONGS,
}


class RepeatMode(StrEnum):
    """Repeat and shuffle modes.

    Only available through the internal ``setSong.php`` endpoint; the open API has no
    equivalent.
    """

    OFF = "off"
    ONE = "one"
    MEDIA_ALL = "media_all"
    MEDIA_SHUFFLE = "media_shuffle"
    ALBUM_ALL = "album_all"
    ALBUM_SHUFFLE = "album_shuffle"
    PLAYLIST_ALL = "playlist_all"
    PLAYLIST_SHUFFLE = "playlist_shuffle"
