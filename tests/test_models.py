"""Tests for parsing what the piano sends back."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    Album,
    CurrentInfo,
    MasterState,
    PlaybackSnapshot,
    PlaybackStatus,
    PowerStatus,
    QuietMode,
    RepeatMode,
    StaticInfo,
)
from aiodisklavier import models as models_module

from .conftest import (
    CURRENT_INFO_PAYLOAD,
    MASTER_PAYLOAD,
    RADIO_CURRENT_INFO_PAYLOAD,
    RADIO_MASTER_PAYLOAD,
    STATIC_INFO_PAYLOAD,
)


def test_models_are_keyword_only() -> None:
    """Models cannot be built positionally, so a field can be added without breaking anyone.

    The firmware's payloads are wider than what is modelled, and twice already a field
    had to be appended with a default purely to keep positional callers working. Keyword
    construction makes field order, and new fields with defaults, nobody's business.
    """
    with pytest.raises(TypeError):
        Album(1, "Pop")  # type: ignore[misc]
    assert Album(album_id=1, title="Pop").title == "Pop"


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


@pytest.mark.parametrize(
    ("field", "attribute"),
    [
        ("power_status", "power_status"),
        ("quiet_status", "quiet_status"),
        ("playback_status", "playback_status"),
    ],
)
def test_current_info_unknown_status_is_none(field: str, attribute: str) -> None:
    """A status this library has no name for is unknown -- not raised, and not guessed.

    These used to fall back to ``ON``, ``ACOUSTIC`` and ``PAUSE``. That went wrong on the
    reference piano itself: it reports ``playback_status: "radio"`` for as long as a radio
    channel plays and ``quiet_status: "headphone"`` with headphones plugged in, and both
    were being read as paused and acoustic. The next value nobody has seen will come from
    a piano nobody here has met, and "on" would be a confident answer to a question this
    library cannot actually answer.
    """
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, field: "hyperdrive"})
    assert getattr(info, attribute) is None


@pytest.mark.parametrize("value", [None, "", [], {}, 7])
def test_current_info_absent_or_junk_status_is_none(value: object) -> None:
    """A missing, empty or mistyped status degrades like an unknown one."""
    payload = {**CURRENT_INFO_PAYLOAD, "quiet_status": value}
    assert CurrentInfo.from_json(payload).quiet_status is None
    del payload["quiet_status"]
    assert CurrentInfo.from_json(payload).quiet_status is None


def test_unknown_status_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    """An unrecognised value is worth one warning, not one per poll.

    State is polled every few seconds, so a piano sitting in a state this library cannot
    name would otherwise fill the log for as long as it stayed there.
    """
    payload = {**CURRENT_INFO_PAYLOAD, "power_status": "defragmenting"}
    with caplog.at_level("WARNING", logger="aiodisklavier.models"):
        for _ in range(3):
            CurrentInfo.from_json(payload)
    warnings = [r for r in caplog.records if "defragmenting" in r.getMessage()]
    assert len(warnings) == 1
    assert "power_status" in warnings[0].getMessage()
    assert "github.com" in warnings[0].getMessage()


def test_absent_status_is_not_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Silence is not news: only a value that is present and unknown is logged."""
    with caplog.at_level("WARNING", logger="aiodisklavier.models"):
        CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "quiet_status": ""})
    assert caplog.records == []


def test_unknown_value_reports_are_bounded(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device cannot grow the log, or the memory behind it, without limit.

    The values are device-supplied, so a broken or hostile piano could send a fresh one
    on every poll. Reporting stops at a ceiling, and long values are clipped.
    """
    monkeypatch.setattr(models_module, "_reported_unknowns", set())
    monkeypatch.setattr(models_module, "_MAX_REPORTED_UNKNOWNS", 2)
    with caplog.at_level("WARNING", logger="aiodisklavier.models"):
        for index in range(5):
            CurrentInfo.from_json(
                {**CURRENT_INFO_PAYLOAD, "power_status": f"state-{index}" + "x" * 500}
            )
    assert len(caplog.records) == 2
    assert all(len(r.getMessage()) < 400 for r in caplog.records)


def test_radio_is_a_real_playback_state() -> None:
    """While a radio channel plays, the open API says so -- and says nothing else.

    From hardware: ``playback_status`` reads ``radio`` and the title, artist, folder,
    position and length all go blank. The programme is only in ``master.json``.
    """
    info = CurrentInfo.from_json(RADIO_CURRENT_INFO_PAYLOAD)
    assert info.playback_status is PlaybackStatus.RADIO
    assert info.is_radio is True
    # The piano is making music, so a media player should say it is playing...
    assert info.is_playing is True
    # ...and position zero must not read as "stopped" while it does.
    assert info.is_stopped is False
    assert info.song_title is None
    assert info.duration_ms == 0


def test_load_is_a_real_playback_state() -> None:
    """``load`` is the sequencer fetching a song: not playing, and not stopped either."""
    info = CurrentInfo.from_json(
        {**CURRENT_INFO_PAYLOAD, "playback_status": "load", "playback_position": "0"}
    )
    assert info.playback_status is PlaybackStatus.LOAD
    assert info.is_playing is False
    assert info.is_radio is False
    assert info.is_stopped is False


def test_headphone_is_a_real_quiet_state() -> None:
    """Plugging headphones in moves ``quiet_status`` to a third value."""
    info = CurrentInfo.from_json({**CURRENT_INFO_PAYLOAD, "quiet_status": "headphone"})
    assert info.quiet_status is QuietMode.HEADPHONE


def test_an_unknown_status_is_neither_playing_nor_stopped() -> None:
    """Unknown is not evidence of anything, in either direction."""
    info = CurrentInfo.from_json(
        {**CURRENT_INFO_PAYLOAD, "playback_status": "scrubbing", "playback_position": 0}
    )
    assert info.is_playing is False
    assert info.is_stopped is False


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
    assert master.library_updated == 1789281118522


def test_master_state_reports_the_radio_programme() -> None:
    """The channel and the song it is on are only to be found in master.json."""
    master = MasterState.from_json(RADIO_MASTER_PAYLOAD)
    assert master.radio_channel == "Complimentary Channel Sampler"
    assert master.radio_title == "Lullaby of Birdland"
    # Radio off: the block is there, with empty strings.
    quiet = MasterState.from_json(
        {**MASTER_PAYLOAD, "radio": {"info": "disconnected", "status": ""}}
    )
    assert quiet.radio_channel is None
    assert quiet.radio_title is None


def test_snapshot_during_radio_does_not_trust_the_sequencer_block() -> None:
    """A snapshot taken during radio records the channel, and not a bogus position.

    From hardware: while radio plays, ``seq`` reports ``status: play`` with ``time``
    advancing -- the *radio's* song -- but ``song_pfix``/``song_id`` still name the library
    song loaded beforehand. Read naively that is "song y24 was playing at 22 s", and
    restoring it would start the old song from an arbitrary place.
    """
    snapshot = PlaybackSnapshot.from_master_json(RADIO_MASTER_PAYLOAD)
    assert snapshot.radio_active is True
    assert snapshot.radio_channel == "Complimentary Channel Sampler"
    # The loaded song is kept: it is what the piano returns to when radio ends.
    assert snapshot.has_song is True
    assert (snapshot.song_prefix, snapshot.song_id) == ("y", 24)
    # But not the radio's clock, and not the radio's 'play'.
    assert snapshot.position_ms == 0
    assert snapshot.was_playing is False


def test_snapshot_of_a_radio_still_connecting() -> None:
    """Radio can be active before it has a channel to name."""
    connecting = {
        **RADIO_MASTER_PAYLOAD["radio"],
        "radio_channel": "",
        "radio_title": "",
    }
    snapshot = PlaybackSnapshot.from_master_json(
        {**RADIO_MASTER_PAYLOAD, "radio": connecting}
    )
    assert snapshot.radio_active is True
    assert snapshot.radio_channel is None


def test_snapshot_without_radio_is_unchanged() -> None:
    """With radio off the sequencer block is taken at its word, as before."""
    snapshot = PlaybackSnapshot.from_master_json(MASTER_PAYLOAD)
    assert snapshot.radio_active is False
    assert snapshot.radio_channel is None
    assert snapshot.position_ms == 516000


def test_master_state_tolerates_missing_blocks() -> None:
    """A partial master.json must not raise."""
    master = MasterState.from_json({})
    assert master.repeat is None
    assert master.metronome_tempo is None
    assert master.library_updated is None


@pytest.mark.parametrize("junk", ["nope", 3, [1, 2], True])
def test_master_state_tolerates_non_dict_blocks(junk: object) -> None:
    """A block that is not an object degrades like an absent one, not a crash.

    master.json is internal and unversioned, so its shape is the one most likely to
    drift across firmware; a string where an object was expected must not raise.
    """
    master = MasterState.from_json(
        {"piano": junk, "sbc": junk, "seq": junk, "apictrl": junk, "radio": junk}
    )
    assert master.radio_channel is None
    assert master.tempo is None
    assert master.headphone_connected is None
    assert master.metronome_tempo is None
    assert master.library_updated is None

    snapshot = PlaybackSnapshot.from_master_json({"seq": junk, "radio": junk})
    assert snapshot.has_song is False
    assert snapshot.was_playing is False
    assert snapshot.radio_active is False


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
