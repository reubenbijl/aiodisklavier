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
PATH_CTRL_SONG_DB: Final = "/ctrl/song.json"

DEFAULT_PORT: Final = 80
DEFAULT_TIMEOUT: Final = 10.0

#: Upper bound on a response body, in bytes. The largest payload the piano actually serves
#: is a full song list at a few hundred kB, so anything past this is not the piano talking.
#: Reads stop here rather than letting a hostile or broken device stream without limit.
MAX_RESPONSE_BYTES: Final = 1024 * 1024

#: Upper bound for the song database alone. ``/ctrl/song.json`` describes every song in
#: every library in one body -- 0.9 MB at two thousand songs on the reference unit, which
#: the general ceiling would already reject. Eight megabytes leaves room for a library
#: several times that size while still refusing an unbounded stream.
MAX_SONG_DB_BYTES: Final = 8 * 1024 * 1024

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

# SMB. The piano exports two shares from an embedded Samba 3.0.37, which predates SMB2
# entirely -- it speaks NT1 and nothing newer. See ``docs/enspire-api.md`` for the
# negotiation trace; it is the reason this library uses pysmb rather than smbprotocol.
#: The writable share. Drop MIDI here and reindex to make it playable.
SHARE_PC_SHARING: Final = "PC Sharing Folder"
#: The controller's own read-only share.
SHARE_ENSPIRE_CONTROLLER: Final = "ENSPIRE Controller"

#: Direct-TCP SMB port. Port 139 is also open, and works if ``direct_tcp`` is turned off.
SMB_PORT: Final = 445
#: Per-operation timeout in seconds. Higher than the HTTP default because a single
#: operation here can be a multi-megabyte transfer rather than a status read.
SMB_TIMEOUT: Final = 30.0
#: The share is served with guest access on stock firmware, so this username is a label
#: rather than a credential. A piano configured with a password takes real ones.
SMB_GUEST_USER: Final = "guest"
#: NetBIOS name this client claims. Only meaningful over port 139; 15 characters max.
SMB_CLIENT_NAME: Final = "aiodisklavier"
#: NetBIOS name assumed for the piano. Unused over direct TCP, where any value is
#: accepted; override it when connecting over port 139.
SMB_SERVER_NAME: Final = "DISKLAVIER"

#: Names never written to the share, matched per path component with :mod:`fnmatch`.
#:
#: The ``._`` entries are the important ones. macOS writes an AppleDouble companion beside
#: every file it copies, the firmware's indexer picks those up as songs in their own right,
#: and loading one silently resets the piano to the first built-in song -- an HTTP 200 with
#: no error anywhere. See ``docs/enspire-api.md`` §7.7.
DEFAULT_EXCLUDES: Final[tuple[str, ...]] = (
    "._*",
    ".DS_Store",
    ".Spotlight-V100",
    ".TemporaryItems",
    ".Trashes",
    ".fseventsd",
    "Thumbs.db",
    "__pycache__",
    ".git",
)

#: Audio extensions the piano accepts as the backing track of an SMF+Audio song.
#:
#: An audio file sharing a MIDI file's basename is not a song of its own: the firmware
#: pairs the two, plays the piano part on the keys and the audio through the speakers, and
#: reports the *audio* file's length as the song duration. Confirmed on hardware for both
#: extensions -- see ``docs/enspire-api.md`` §8.
AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset({".mp3", ".wav"})

#: File extensions worth putting on the share, as ``suffixes`` for a mirror.
#:
#: Audio is in here deliberately. Filtering a library down to ``{".mid"}`` looks right and
#: leaves every transcription playing as a bare piano part with its backing track missing --
#: which sounds like a working sync, because it is one.
PLAYABLE_SUFFIXES: Final[frozenset[str]] = (
    frozenset({".mid", ".midi", ".kar"}) | AUDIO_SUFFIXES
)

#: How many directory levels below the share root the firmware's indexer descends.
#:
#: Two, exactly. ``<folder>/<subfolder>/song.mid`` is indexed and the subfolder shows up as
#: an album; one level deeper and the file is copied fine, listed fine over SMB, and simply
#: never appears in the library -- no error, from either the copy or the reindex. Established
#: by planting the same file at three depths and reindexing: see ``docs/enspire-api.md`` §8.
#:
#: A file directly in the share root counts as depth zero, so the deepest indexable path is
#: ``a/b/song.mid``.
INDEXED_DEPTH_LIMIT: Final = 2

#: Modification times either side of a copy are compared with this much slack, in seconds.
#: FAT-derived filesystems keep two-second resolution, so an exact comparison would call
#: an unchanged file modified on every pass.
MTIME_TOLERANCE: Final = 2.0

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


class SongFormat(StrEnum):
    """A song's media format, as the song database's own vocabulary spells it.

    These are the values ``/ctrl/song.json`` reports per song, and exactly what the
    controller's own UI switches its format badges on. ``SMF,WAV`` and ``WAV`` never
    appeared in the reference library, but the controller handles them, so they are
    carried here too.
    """

    SMF = "SMF"
    SMF_SOLO = "SMFSOLO"
    SMF_XG = "SMFXG"
    SMF_WAV = "SMF,WAV"
    SMF_MP3 = "SMF,MP3"
    WAV = "WAV"

    @property
    def has_audio(self) -> bool:
        """Whether playing this format sends sound to the speakers, not just the keys.

        The audio-pair formats carry a recorded backing track, and ``SMF_XG`` scores its
        accompaniment on the internal XG tone generator; either way the speaker path is
        in use, and anything wired to it -- an amplifier, a receiver -- wants switching
        on. ``SMF_SOLO`` and plain ``SMF`` drive only the keys. A plain ``SMF`` could in
        principle still hold ensemble channels for the tone generator to voice; the
        database cannot see inside the file, so this keeps the keys-only reading.
        """
        return self in _AUDIO_FORMATS


_AUDIO_FORMATS: Final[frozenset[SongFormat]] = frozenset(
    {SongFormat.SMF_XG, SongFormat.SMF_WAV, SongFormat.SMF_MP3, SongFormat.WAV}
)


class SearchKind(StrEnum):
    """What a :class:`aiodisklavier.models.SearchResult` points at."""

    SONG = "song"
    PLAYLIST = "playlist"
    RADIO = "radio"


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
