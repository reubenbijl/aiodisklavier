"""Typed models for the data the Disklavier returns.

The firmware sends every scalar as a JSON string, including numbers. These models do the
conversion once so callers never have to think about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import PlaybackStatus, PowerStatus, QuietMode, RepeatMode


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
    return candidates[0] if len(candidates) == 1 else None


def _str_or_none(value: Any) -> str | None:
    """Return a non-empty string, or ``None``.

    The firmware uses ``""`` where it means "absent" -- an unknown artist, for instance.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class CurrentInfo:
    """Live state, from ``/api/current_info``.

    One poll of this is enough to drive a media player entity.
    """

    power_status: PowerStatus
    quiet_status: QuietMode
    playback_status: PlaybackStatus
    position_ms: int | None
    volume: int | None
    song_title: str | None
    song_artist: str | None
    song_folder: str | None
    duration_ms: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CurrentInfo:
        """Build from a decoded ``/api/current_info`` payload.

        Unrecognised enum values fall back to a sensible default rather than raising, so a
        future firmware adding a state cannot break an existing integration.
        """
        try:
            power = PowerStatus(data.get("power_status", ""))
        except ValueError:
            power = PowerStatus.ON

        try:
            quiet = QuietMode(data.get("quiet_status", ""))
        except ValueError:
            quiet = QuietMode.ACOUSTIC

        try:
            playback = PlaybackStatus(data.get("playback_status", ""))
        except ValueError:
            playback = PlaybackStatus.PAUSE

        return cls(
            power_status=power,
            quiet_status=quiet,
            playback_status=playback,
            position_ms=_int_or_none(data.get("playback_position")),
            volume=_int_or_none(data.get("volume_main")),
            song_title=_str_or_none(data.get("song_title")),
            song_artist=_str_or_none(data.get("song_artist")),
            song_folder=_str_or_none(data.get("song_folder")),
            duration_ms=_int_or_none(data.get("song_length")),
        )

    @property
    def is_playing(self) -> bool:
        """Whether the piano is actively playing."""
        return self.playback_status is PlaybackStatus.PLAY

    @property
    def is_stopped(self) -> bool:
        """Whether the piano looks stopped rather than paused mid-song.

        The firmware has no distinct stop state: ``stop`` leaves ``playback_status`` at
        ``pause`` and rewinds to zero. Position is the only signal available.
        """
        return not self.is_playing and not self.position_ms

    @property
    def position_seconds(self) -> float | None:
        """Playback position in seconds."""
        return None if self.position_ms is None else self.position_ms / 1000

    @property
    def duration_seconds(self) -> float | None:
        """Song length in seconds."""
        return None if self.duration_ms is None else self.duration_ms / 1000


@dataclass(frozen=True, slots=True)
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

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MasterState:
        """Build from a decoded ``/ctrl/master.json`` payload."""
        piano = _dict_or_empty(data.get("piano"))
        sbc = _dict_or_empty(data.get("sbc"))
        seq = _dict_or_empty(data.get("seq"))

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
        )


@dataclass(frozen=True, slots=True)
class PlaybackSnapshot:
    """A restorable playback position, captured from ``/ctrl/master.json``.

    Enough to put the piano back where it was after an interruption: which song was loaded,
    how far in, and whether it was playing. Reselecting a song needs both the library prefix
    and the song id, and ``/api/current_info`` exposes neither -- which is why this reads the
    internal ``master.json`` instead.

    See :meth:`aiodisklavier.Disklavier.async_snapshot_playback` and
    :meth:`aiodisklavier.Disklavier.async_restore_playback`.
    """

    song_prefix: str | None
    song_id: int | None
    position_ms: int
    was_playing: bool

    @classmethod
    def from_master_json(cls, data: dict[str, Any]) -> PlaybackSnapshot:
        """Build from a decoded ``/ctrl/master.json`` payload."""
        seq = _dict_or_empty(data.get("seq"))
        return cls(
            song_prefix=_str_or_none(seq.get("song_pfix")),
            song_id=_int_or_none(seq.get("song_id")),
            position_ms=_int_or_none(seq.get("time")) or 0,
            # The sequencer reports 'play' only while actively playing; 'pause', 'stop' and
            # 'load' all mean quiet.
            was_playing=seq.get("status") == "play",
        )

    @property
    def has_song(self) -> bool:
        """Whether a song was loaded and can therefore be reselected."""
        return bool(self.song_prefix) and self.song_id is not None


@dataclass(frozen=True, slots=True)
class Song:
    """A song in one of the libraries."""

    song_id: int
    title: str


@dataclass(frozen=True, slots=True)
class Album:
    """An album in one of the libraries."""

    album_id: int
    title: str


@dataclass(frozen=True, slots=True)
class Playlist:
    """A playlist."""

    playlist_id: int
    title: str


@dataclass(frozen=True, slots=True)
class RadioChannel:
    """A DisklavierRadio channel."""

    channel_id: int
    title: str
