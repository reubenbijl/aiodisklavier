"""Constants and enumerations for the Disklavier ENSPIRE local API.

Values here mirror the firmware exactly. See ``docs/enspire-api.md`` for provenance.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
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

# Timing for :meth:`aiodisklavier.Disklavier.async_restore_playback`. The sequencer takes
# about two seconds to load a song and drops whatever it is sent meanwhile -- a seek or a
# ``play`` issued straight after ``load_song`` answers HTTP 200 and is lost. Measured on
# hardware: ``load`` first showed in ``current_info`` 0.7-1.1 s after the command and
# cleared 1.3-2.0 s after it, for a plain MIDI file and an MP3-backed one alike.
#: How long to leave the sequencer after a ``stop`` before sending it a ``load_song``.
#:
#: A precaution, and labelled as one: what it guards against has not been pinned down. The
#: sequencer daemon on the reference piano can wedge -- it goes on accepting a selection and
#: never finishes loading it, and only a reboot clears that (see CONTRIBUTING.md). It
#: wedged twice in one session of hardware testing, both times during sequences that sent
#: ``stop`` and ``load_song`` a few tens of milliseconds apart, the second time with nothing
#: else going on; the same two commands sent a second apart, many times that day, never
#: did it. Two incidents are not proof, but a second is cheap.
SEQUENCER_SETTLE: Final = 1.0
#: How long to leave a freshly cued song before asking whether it has loaded. State lags a
#: command, so a poll any sooner sees the *previous* state, which also reads as not loading.
LOAD_SETTLE: Final = 1.5
#: How long to wait for ``load`` to clear before carrying on regardless.
LOAD_TIMEOUT: Final = 6.0
#: How often to check whether it has.
LOAD_POLL_INTERVAL: Final = 0.25
#: A seek or ``play`` sent to a cued song is checked against what the piano then reports,
#: and sent again if it did not take -- once. Deliberately no more than that: the sequencer
#: daemon is fragile (see CONTRIBUTING.md), a command it has ignored twice is not going to
#: be obeyed a third time, and repeating into a daemon in trouble is how to make it worse.
RESTORE_ATTEMPTS: Final = 2
#: How long after sending to look. Long enough for the state files to catch up.
RESTORE_CHECK_DELAY: Final = 0.75
#: A seek counts as kept within this many milliseconds. Position resolution is about a
#: second, so an exact comparison would call a good seek a failed one.
SEEK_TOLERANCE_MS: Final = 1500

# Defaults for :meth:`aiodisklavier.Disklavier.async_play_radio`.
#: How long to wait for a channel to finish connecting before returning regardless. The
#: piano took four to seven seconds from the request to the first title on hardware.
RADIO_CONNECT_TIMEOUT: Final = 20.0
#: How often to check whether it has.
RADIO_CONNECT_POLL_INTERVAL: Final = 0.5

# Defaults for :meth:`aiodisklavier.Disklavier.async_stop_radio`.
#: How long to wait for the piano to finish leaving radio before carrying on regardless.
#: It took a little over a second on hardware; this is headroom, not an expectation.
RADIO_EXIT_TIMEOUT: Final = 5.0
#: How often to check whether it has.
RADIO_EXIT_POLL_INTERVAL: Final = 0.25

#: UPnP device type advertised over SSDP, used for discovery.
UPNP_DEVICE_TYPE: Final = "urn:schemas-upnp-org:device:Disklavier:1"

#: Where to send a report. Named in the warning logged for a value this library does not
#: recognise, and by ``python -m aiodisklavier``, because the most useful reports are the
#: ones from pianos this library has never met.
ISSUES_URL: Final = "https://github.com/reubenbijl/aiodisklavier/issues"

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

    ``LOAD`` is transitional: the sequencer reports it for a second or two while it loads a
    song, including on the way out of radio. ``RADIO`` stands for the whole time a
    DisklavierRadio channel is active, during which the rest of ``current_info`` goes
    blank -- no title, no position, no length -- and the programme is only to be found in
    :class:`~aiodisklavier.MasterState`.

    The sequencer has more states than these (recording, fast-forward, synchronised
    playback), and whether the open API reports any of them has not been observed. A value
    not listed here parses to ``None`` rather than to a guess.
    """

    PLAY = "play"
    PAUSE = "pause"
    LOAD = "load"
    RADIO = "radio"


class QuietMode(StrEnum):
    """Whether the hammers physically strike the strings.

    ``HEADPHONE`` is reported, never requested: it is what the piano says while headphones
    are plugged in, and asking for it answers HTTP 400. Only ``ACOUSTIC`` and ``QUIET`` can
    be set.
    """

    ACOUSTIC = "acoustic"
    QUIET = "quiet"
    HEADPHONE = "headphone"


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
    """What a :class:`aiodisklavier.SearchResult` points at."""

    SONG = "song"
    PLAYLIST = "playlist"
    RADIO = "radio"


#: ``master.json`` reports the current library as a bare one-letter prefix, while the open API
#: takes the long-form group name. This maps back, so a snapshot taken from the internal
#: endpoint can be restored through the open API. Read-only: it describes the firmware, and
#: a caller that needs a different table should build its own.
PREFIX_TO_SONG_GROUP: Final[Mapping[str, SongGroup]] = MappingProxyType(
    {
        "d": SongGroup.BUILT_IN_SONGS,
        "l": SongGroup.BUILT_IN_PLAYLIST,
        # Established from the song database rather than from a loaded song, because My
        # Songs was empty on the reference unit: its one album is id 4 in
        # ``get_album_list&group=my_songs`` and is keyed ``s4`` in ``song.json``, with every
        # other prefix accounted for by another library. See ``docs/enspire-api.md`` §4.
        "s": SongGroup.MY_SONGS,
        "r": SongGroup.MY_RECORDINGS,
        "f": SongGroup.PC_SHARING_FOLDER,
        "y": SongGroup.DOWNLOADED_SONGS,
    }
)


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
