"""Tests for parsing what the piano sends back."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    CurrentInfo,
    MasterState,
    PlaybackSnapshot,
    PlaybackStatus,
    PowerStatus,
    QuietMode,
    RepeatMode,
    StaticInfo,
)

from .conftest import CURRENT_INFO_PAYLOAD, MASTER_PAYLOAD, STATIC_INFO_PAYLOAD


def test_static_info_parses() -> None:
    """A static_info payload maps onto the model."""
    info = StaticInfo.from_json(STATIC_INFO_PAYLOAD)
    assert info.disklavier_id == "DKV000000000000"
    assert info.version == "5.24.00"
    assert info.model == "PRO"
    assert info.piano_type == "grand"


def test_current_info_parses_string_numbers() -> None:
    """The firmware sends numbers as strings; they come out as ints."""
    info = CurrentInfo.from_json(CURRENT_INFO_PAYLOAD)
    assert info.power_status is PowerStatus.ON
    assert info.playback_status is PlaybackStatus.PAUSE
    assert info.quiet_status is QuietMode.ACOUSTIC
    assert info.position_ms == 516000
    assert info.duration_ms == 851900
    assert info.volume == 100
    assert info.position_seconds == pytest.approx(516.0)
    assert info.duration_seconds == pytest.approx(851.9)


def test_current_info_empty_strings_become_none() -> None:
    """The firmware uses an empty string to mean 'absent'."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "song_artist": ""})
    assert info.song_artist is None


def test_current_info_unknown_enum_falls_back() -> None:
    """A future firmware state must not raise."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "power_status": "hyperdrive"})
    assert info.power_status is PowerStatus.ON


def test_current_info_unknown_quiet_mode_falls_back() -> None:
    """An unrecognised quiet mode falls back to acoustic."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "quiet_status": "whisper"})
    assert info.quiet_status is QuietMode.ACOUSTIC


def test_current_info_unknown_playback_status_falls_back() -> None:
    """An unrecognised playback status falls back to paused, never to playing.

    Guessing 'playing' would have a media player claim the piano is sounding when it may
    not be; paused is the safe direction to be wrong in.
    """
    info = CurrentInfo.from_json(
        {**CURRENT_INFO_PAYLOAD, "playback_status": "scrubbing"}
    )
    assert info.playback_status is PlaybackStatus.PAUSE


@pytest.mark.parametrize("value", ["abc", [], {}])
def test_non_numeric_values_become_none(value: object) -> None:
    """Junk where a number was expected yields None rather than raising."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "playback_position": value})
    assert info.position_ms is None


def test_wakeup_is_a_real_power_state() -> None:
    """Waking is a distinct state, not on or sleep."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "power_status": "wakeup"})
    assert info.power_status is PowerStatus.WAKEUP


@pytest.mark.parametrize(
    ("status", "position", "playing", "stopped"),
    [
        ("play", "1000", True, False),
        ("pause", "516000", False, False),
        # 'stop' leaves the firmware reporting pause at position zero.
        ("pause", "0", False, True),
    ],
)
def test_is_playing_and_is_stopped(
    status: str, position: str, playing: bool, stopped: bool
) -> None:
    """Stopped is inferred from position, since the firmware has no stop state."""
    info = CurrentInfo.from_json(
        {
            **CURRENT_INFO_PAYLOAD,
            "playback_status": status,
            "playback_position": position,
        }
    )
    assert info.is_playing is playing
    assert info.is_stopped is stopped


def test_master_state_parses() -> None:
    """The extended state exposes repeat and the metronome."""
    master = MasterState.from_json(MASTER_PAYLOAD)
    assert master.repeat is RepeatMode.OFF
    assert master.headphone_connected is False
    assert master.metronome_enabled is False
    assert master.metronome_tempo == 120
    assert master.metronome_beat == "4/4"
    assert master.key_motion is True
    assert master.tempo == 100


def test_master_state_tolerates_missing_blocks() -> None:
    """A partial master.json must not raise."""
    master = MasterState.from_json({})
    assert master.repeat is None
    assert master.metronome_tempo is None


@pytest.mark.parametrize("junk", ["nope", 3, [1, 2], True])
def test_master_state_tolerates_non_dict_blocks(junk: object) -> None:
    """A block that is not an object degrades like an absent one, not a crash.

    master.json is internal and unversioned, so its shape is the one most likely to
    drift across firmware; a string where an object was expected must not raise.
    """
    master = MasterState.from_json({"piano": junk, "sbc": junk, "seq": junk})
    assert master.tempo is None
    assert master.headphone_connected is None
    assert master.metronome_tempo is None

    snapshot = PlaybackSnapshot.from_master_json({"seq": junk})
    assert snapshot.has_song is False
    assert snapshot.was_playing is False


def test_master_state_unknown_repeat_is_none() -> None:
    """An unrecognised repeat mode is reported as unknown rather than guessed."""
    master = MasterState.from_json({**MASTER_PAYLOAD, "repeat": "sideways"})
    assert master.repeat is None


def test_master_state_recovers_truncated_repeat() -> None:
    """The firmware clips the one 16-character repeat mode to 15 characters.

    Setting ``playlist_shuffle`` reads back from master.json as ``playlist_shuffl``.
    Confirmed on hardware, stable across repeated reads, and the command itself is accepted
    correctly -- so only the state file is wrong.
    """
    master = MasterState.from_json({**MASTER_PAYLOAD, "repeat": "playlist_shuffl"})
    assert master.repeat is RepeatMode.PLAYLIST_SHUFFLE


def test_master_state_ambiguous_prefix_is_none() -> None:
    """A clipped value that could be more than one mode is not guessed at."""
    # 'playlist_' prefixes both playlist_all and playlist_shuffle.
    master = MasterState.from_json({**MASTER_PAYLOAD, "repeat": "playlist_"})
    assert master.repeat is None


@pytest.mark.parametrize("value", [None, "", 42])
def test_master_state_non_string_repeat_is_none(value: object) -> None:
    """A missing or non-string repeat value is unknown, not an error."""
    master = MasterState.from_json({**MASTER_PAYLOAD, "repeat": value})
    assert master.repeat is None
