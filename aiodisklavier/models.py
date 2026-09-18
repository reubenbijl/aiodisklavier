"""Typed models for the data the Disklavier returns.

The firmware sends every scalar as a JSON string, including numbers. These models do the
conversion once so callers never have to think about it.

Every model is keyword-only. The firmware's payloads are wider than what is modelled here,
and a field for something newly understood has to be addable without reordering anyone's
arguments -- so new fields arrive with defaults, and code that builds these models, in a
test fixture say, keeps working across releases.

**What is not recognised is ``None``, never a guess.** A value outside an enumeration
parses to ``None`` and is logged once, rather than being mapped onto the nearest plausible
member. This library was written against one piano, so the first value it has never seen
will come from somebody else's -- and reading that as "on", or "paused", would be a
confident answer to a question it could not actually answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, TypeVar

from .const import (
    ISSUES_URL,
    PREFIX_TO_SONG_GROUP,
    PlaybackStatus,
    PlaylistGroup,
    PowerStatus,
    QuietMode,
    RepeatMode,
    SearchKind,
    SongFormat,
    SongGroup,
)

_LOGGER = logging.getLogger(__name__)

_E = TypeVar("_E", bound=StrEnum)

#: Unrecognised values already reported, as ``(field, value)``. State arrives on every poll,
#: so without this a piano sitting in a state this library has no name for would log the
#: same line every few seconds for as long as it stayed there.
_reported_unknowns: set[tuple[str, str]] = set()

#: Stop reporting after this many distinct values. The values are device-supplied, and a
#: broken or hostile device must not be able to grow the set, or the log, without limit.
_MAX_REPORTED_UNKNOWNS: Final = 32

#: How much of an unrecognised value to keep and to log.
_UNKNOWN_VALUE_CHARS: Final = 80


def _report_unknown(name: str, value: Any) -> None:
    """Log a value this library has no name for, once per distinct value."""
    key = (name, str(value)[:_UNKNOWN_VALUE_CHARS])
    if key in _reported_unknowns or len(_reported_unknowns) >= _MAX_REPORTED_UNKNOWNS:
        return
    _reported_unknowns.add(key)
    _LOGGER.warning(
        "The Disklavier reported %s=%r, which aiodisklavier does not recognise; "
        "treating it as unknown. Please report it at %s",
        name,
        key[1],
        ISSUES_URL,
    )


def _enum_or_none(enum: type[_E], value: Any, name: str) -> _E | None:
    """Read an enumerated value: ``None`` when absent, and when not recognised.

    Absent -- a missing key or the firmware's ``""`` -- is unremarkable and silent. A
    value that is present and unknown is news, and is reported.
    """
    if value is None or value == "":
        return None
    try:
        return enum(value)
    except ValueError:
        _report_unknown(name, value)
        return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """Return a mapping as-is, and anything else as empty.

    ``master.json`` is internal and unversioned; a block that is not the object this
    code expects must degrade to "absent", not crash the parse.
    """
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    """Coerce a firmware value to ``int``, tolerating empty strings and junk."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _repeat_mode(value: Any) -> RepeatMode | None:
    """Read a repeat mode, working around a firmware truncation bug.

    ``master.json`` reports the repeat mode through a fixed-width field that holds only 15
    characters, so the one 16-character mode comes back clipped: setting
    ``playlist_shuffle`` reads back as ``playlist_shuffl``. The command itself is accepted
    correctly -- only the state file is wrong -- so a clipped value is resolved back to the
    single mode it can only have been.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return RepeatMode(value)
    except ValueError:
        pass
    candidates = [mode for mode in RepeatMode if mode.value.startswith(value)]
    if len(candidates) == 1:
        return candidates[0]
    _report_unknown("repeat", value)
    return None


def _str_or_none(value: Any) -> str | None:
    """Return a non-empty string, or ``None``.

    The firmware uses ``""`` where it means "absent" -- an unknown artist, for instance.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True, kw_only=True)
class StaticInfo:
    """Device identity, from ``/api/static_info``.

    This does not change while the piano is running, so it only needs fetching once.
    """

    api_version: str
    api_revision: str
    disklavier_id: str
    region: str
    version: str
    model: str
    piano_type: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StaticInfo:
        """Build from a decoded ``/api/static_info`` payload."""
        return cls(
            api_version=str(data.get("api_version", "")),
            api_revision=str(data.get("api_revision", "")),
            disklavier_id=str(data.get("disklavier_id", "")),
            region=str(data.get("enspire_region", "")),
            version=str(data.get("enspire_version", "")),
            model=str(data.get("enspire_model", "")),
            piano_type=str(data.get("piano_type", "")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentInfo:
    """Live state, from ``/api/current_info``.

    One poll of this is enough to drive a media player entity -- except while a radio
    channel is playing, when the title, position and length here all go blank and the
    programme is in :class:`MasterState` instead. :attr:`is_radio` says when.
    """

    #: ``None`` when the piano reported a value this library does not recognise, or none at
    #: all. The same goes for the two statuses below; a model without a silent system may
    #: well have nothing to say for ``quiet_status``.
    power_status: PowerStatus | None
    quiet_status: QuietMode | None
    playback_status: PlaybackStatus | None
    position_ms: int | None
    volume: int | None
    song_title: str | None
    song_artist: str | None
    song_folder: str | None
    duration_ms: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CurrentInfo:
        """Build from a decoded ``/api/current_info`` payload.

        An unrecognised status parses to ``None`` rather than raising, so a firmware that
        adds a state cannot break an existing integration -- and rather than falling back to
        a plausible member, so it cannot quietly mislead one either.
        """
        return cls(
            power_status=_enum_or_none(
                PowerStatus, data.get("power_status"), "power_status"
            ),
            quiet_status=_enum_or_none(
                QuietMode, data.get("quiet_status"), "quiet_status"
            ),
            playback_status=_enum_or_none(
                PlaybackStatus, data.get("playback_status"), "playback_status"
            ),
            position_ms=_int_or_none(data.get("playback_position")),
            volume=_int_or_none(data.get("volume_main")),
            song_title=_str_or_none(data.get("song_title")),
            song_artist=_str_or_none(data.get("song_artist")),
            song_folder=_str_or_none(data.get("song_folder")),
            duration_ms=_int_or_none(data.get("song_length")),
        )

    @property
    def is_playing(self) -> bool:
        """Whether the piano is making music: playing a song, or playing the radio.

        ``False`` for a status this library does not recognise. Claiming the piano is
        sounding when it may not be is the worse direction to be wrong in.
        """
        return self.playback_status in (PlaybackStatus.PLAY, PlaybackStatus.RADIO)

    @property
    def is_radio(self) -> bool:
        """Whether a DisklavierRadio channel is active.

        Worth checking before sending a transport command. While radio is on the piano
        answers ``play``, ``pause``, ``stop``, ``next_song`` and every ``load_*`` and
        ``play_*`` with HTTP 200 and ignores them; only
        :meth:`~aiodisklavier.Disklavier.async_stop_radio` ends it.
        """
        return self.playback_status is PlaybackStatus.RADIO

    @property
    def is_stopped(self) -> bool:
        """Whether the piano looks stopped rather than paused mid-song.

        The firmware has no distinct stop state: ``stop`` leaves ``playback_status`` at
        ``pause`` and rewinds to zero. Position is the only signal available. A piano that
        is loading, or in a state this library does not recognise, is not called stopped.
        """
        return self.playback_status is PlaybackStatus.PAUSE and not self.position_ms

    @property
    def position_seconds(self) -> float | None:
        """Playback position in seconds."""
        return None if self.position_ms is None else self.position_ms / 1000

    @property
    def duration_seconds(self) -> float | None:
        """Song length in seconds."""
        return None if self.duration_ms is None else self.duration_ms / 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class MasterState:
    """The subset of ``/ctrl/master.json`` that the open API does not expose.

    This comes from the piano's internal, unversioned endpoint, so treat it as best-effort:
    callers should tolerate it being unavailable rather than depend on it.
    """

    repeat: RepeatMode | None
    headphone_connected: bool | None
    metronome_enabled: bool | None
    metronome_tempo: int | None
    metronome_beat: str | None
    key_motion: bool | None
    tempo: int | None
    #: Which library the loaded song lives in, as the sequencer's one-letter prefix.
    #: Together with :attr:`song_id` this keys the song database -- see
    #: :meth:`aiodisklavier.Disklavier.async_lookup_song`.
    song_prefix: str | None
    song_id: int | None
    #: When the song library last changed, in milliseconds on the piano's clock
    #: (``apictrl.update_window``). The firmware moves it when a reindex finishes and
    #: leaves it alone for transport and volume changes, so a new value means any
    #: listing read before it may be out of date.
    library_updated: int | None = None
    #: The DisklavierRadio channel that is playing, by title, and the song it is on. This
    #: is the only place the programme is reported: ``current_info`` goes blank for as long
    #: as radio is active. Both are ``None`` when radio is off, and for the few seconds a
    #: channel spends connecting.
    #:
    #: Do not read :attr:`song_prefix` and :attr:`song_id` as what is sounding while these
    #: are set -- during radio they still name the library song that was loaded before it.
    radio_channel: str | None = None
    radio_title: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MasterState:
        """Build from a decoded ``/ctrl/master.json`` payload."""
        piano = _dict_or_empty(data.get("piano"))
        sbc = _dict_or_empty(data.get("sbc"))
        seq = _dict_or_empty(data.get("seq"))
        apictrl = _dict_or_empty(data.get("apictrl"))
        radio = _dict_or_empty(data.get("radio"))

        repeat = _repeat_mode(data.get("repeat"))

        headphone = sbc.get("headphone")
        metronome = piano.get("met_status")
        key_motion = piano.get("key_motion")

        return cls(
            repeat=repeat,
            headphone_connected=(
                None if headphone is None else headphone == "connected"
            ),
            # 'disable' means the metronome is unavailable, not merely switched off.
            metronome_enabled=(
                None if metronome is None else metronome not in ("disable", "off")
            ),
            metronome_tempo=_int_or_none(piano.get("met_tempo")),
            metronome_beat=_str_or_none(piano.get("met_beat")),
            key_motion=None if key_motion is None else key_motion == "on",
            tempo=_int_or_none(seq.get("tempo")),
            song_prefix=_str_or_none(seq.get("song_pfix")),
            song_id=_int_or_none(seq.get("song_id")),
            library_updated=_int_or_none(apictrl.get("update_window")),
            radio_channel=_str_or_none(radio.get("radio_channel")),
            radio_title=_str_or_none(radio.get("radio_title")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackSnapshot:
    """A restorable playback position, captured from ``/ctrl/master.json``.

    Enough to put the piano back where it was after an interruption: which song was loaded,
    how far in, and whether it was playing -- or which radio channel was on. Reselecting a
    song needs both the library prefix and the song id, and ``/api/current_info`` exposes
    neither, which is why this reads the internal ``master.json`` instead.

    See :meth:`aiodisklavier.Disklavier.async_snapshot_playback` and
    :meth:`aiodisklavier.Disklavier.async_restore_playback`.
    """

    song_prefix: str | None
    song_id: int | None
    position_ms: int
    was_playing: bool
    #: Whether DisklavierRadio was active. It has to be ended before anything else will
    #: play: the piano ignores every ``load_*`` and ``play_*`` command while it is on.
    radio_active: bool = False
    #: The channel that was playing, by title -- the state file names it no other way.
    #: ``None`` when radio was off, and when it was caught still connecting.
    radio_channel: str | None = None

    @classmethod
    def from_master_json(cls, data: dict[str, Any]) -> PlaybackSnapshot:
        """Build from a decoded ``/ctrl/master.json`` payload."""
        seq = _dict_or_empty(data.get("seq"))
        radio = _dict_or_empty(data.get("radio"))
        # The radio block's status is "" with radio off, and "channel" or "play" with it on.
        radio_active = _str_or_none(radio.get("status")) is not None
        return cls(
            song_prefix=_str_or_none(seq.get("song_pfix")),
            song_id=_int_or_none(seq.get("song_id")),
            # During radio the sequencer block keeps naming the library song loaded
            # beforehand, while its clock and status follow the *radio's* current song.
            # Taken at face value that pairs one song's id with another's position, and
            # restoring it would start the old song playing from somewhere arbitrary. The
            # song itself is worth keeping -- it is what the piano returns to when radio
            # ends -- but entering radio had already rewound it, so: from the top, quiet.
            position_ms=0 if radio_active else _int_or_none(seq.get("time")) or 0,
            # The sequencer reports 'play' only while actively playing; 'pause', 'stop' and
            # 'load' all mean quiet.
            was_playing=not radio_active and seq.get("status") == "play",
            radio_active=radio_active,
            radio_channel=(
                _str_or_none(radio.get("radio_channel")) if radio_active else None
            ),
        )

    @property
    def has_song(self) -> bool:
        """Whether a song was loaded and can therefore be reselected."""
        return bool(self.song_prefix) and self.song_id is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class LibrarySong:
    """One song as the piano's own database describes it, from ``/ctrl/song.json``.

    Richer than the open API's :class:`Song` rows: the database is the controller UI's
    backing store, and it carries what the listings omit -- most usefully the media
    :class:`~aiodisklavier.SongFormat`, which is how the controller knows to show
    a format badge and to lock the tempo control for audio-driven songs.
    """

    prefix: str
    song_id: int
    title: str
    format: SongFormat | None
    group: SongGroup | None
    album_id: int | None
    length_ms: int | None
    genre: str | None
    composer: str | None
    performer: str | None

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> LibrarySong | None:
        """Build from one database row, or ``None`` for a row missing its identity."""
        prefix = _str_or_none(row.get("pfix"))
        song_id = _int_or_none(row.get("song_id"))
        if prefix is None or song_id is None:
            return None

        return cls(
            prefix=prefix,
            song_id=song_id,
            title=str(row.get("song_title", "")),
            format=_enum_or_none(SongFormat, row.get("format"), "format"),
            group=PREFIX_TO_SONG_GROUP.get(prefix),
            album_id=_int_or_none(row.get("album_id")),
            length_ms=_int_or_none(row.get("length")),
            genre=_str_or_none(row.get("genre")),
            composer=_str_or_none(row.get("composer")),
            performer=_str_or_none(row.get("performer")),
        )

    @property
    def has_audio(self) -> bool | None:
        """Whether playing this song uses the speaker path, or ``None`` if unknown.

        See :attr:`aiodisklavier.SongFormat.has_audio` for the rule.
        """
        return None if self.format is None else self.format.has_audio


@dataclass(frozen=True, slots=True, kw_only=True)
class LibraryAlbum:
    """One album as the piano's own database describes it, from ``/ctrl/song.json``.

    The same albums :meth:`~aiodisklavier.Disklavier.async_get_albums` lists, under the
    same ids, but every library's albums arrive in the one database fetch, which the piano
    serves several times faster than it builds an album listing.

    Titles agree where they are the user's own: a PC Sharing Folder album is titled by its
    folder's path on the share in both. The folders the firmware provides are named
    differently by the two sources -- a library's root is ``(Root)`` here and ``""`` in the
    listing, and My Recordings' pair are ``Temporary Folder`` and ``Keep`` here against
    ``Recorded Songs`` and ``Kept Songs`` there -- so join on the id, not the title.
    """

    prefix: str
    album_id: int
    title: str
    #: Where the album lives on the piano's own storage; for a PC Sharing Folder album,
    #: ``FromToPC/`` followed by its path on the share.
    path: str | None
    group: SongGroup | None

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> LibraryAlbum | None:
        """Build from one database row, or ``None`` for a row missing its identity."""
        prefix = _str_or_none(row.get("pfix"))
        album_id = _int_or_none(row.get("album_id"))
        if prefix is None or album_id is None:
            return None

        return cls(
            prefix=prefix,
            album_id=album_id,
            title=str(row.get("album_title", "")),
            path=_str_or_none(row.get("album_path")),
            group=PREFIX_TO_SONG_GROUP.get(prefix),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SongDatabase:
    """The piano's own song database, from ``/ctrl/song.json``.

    One fetch describes every song in every library. Entries are keyed the way the
    database keys them -- library prefix immediately followed by the song id, ``d1`` or
    ``f3608`` -- which is also how ``master.json``'s sequencer block names the loaded
    song, so a lookup joins the two directly.
    """

    #: The database's own change counter. The firmware bumps it when the library is
    #: re-indexed, so two fetches reporting the same value describe the same library.
    update: int | None
    songs: dict[str, LibrarySong]
    #: Keyed like :attr:`songs`: library prefix, then album id -- ``f323``.
    albums: dict[str, LibraryAlbum] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SongDatabase:
        """Build from a decoded ``/ctrl/song.json`` payload."""
        songs: dict[str, LibrarySong] = {}
        for key, row in _dict_or_empty(data.get("song")).items():
            song = LibrarySong.from_json(_dict_or_empty(row))
            if song is not None:
                songs[str(key)] = song

        albums: dict[str, LibraryAlbum] = {}
        for key, row in _dict_or_empty(data.get("album")).items():
            album = LibraryAlbum.from_json(_dict_or_empty(row))
            if album is not None:
                albums[str(key)] = album

        return cls(update=_int_or_none(data.get("update")), songs=songs, albums=albums)

    def lookup(self, prefix: str, song_id: int) -> LibrarySong | None:
        """Find one song by the identity the sequencer reports for it."""
        return self.songs.get(f"{prefix}{song_id}")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    """One search hit, carrying whichever reference its kind needs to play it.

    A ``SONG`` result holds :attr:`song` (play with
    :meth:`~aiodisklavier.Disklavier.async_play_song` using its id and group); a
    ``PLAYLIST`` result holds :attr:`playlist` and :attr:`playlist_group`; a ``RADIO``
    result holds :attr:`channel`.
    """

    kind: SearchKind
    title: str
    #: Match quality, 0 exclusive to 1 inclusive; results come back best-first.
    score: float
    song: LibrarySong | None = None
    playlist: Playlist | None = None
    playlist_group: PlaylistGroup | None = None
    channel: RadioChannel | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Song:
    """A song in one of the libraries."""

    song_id: int
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Album:
    """An album in one of the libraries."""

    album_id: int
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Playlist:
    """A playlist."""

    playlist_id: int
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RadioChannel:
    """A DisklavierRadio channel."""

    channel_id: int
    title: str
