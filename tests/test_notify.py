"""Tests for snapshot, restore, and one-shot notifications."""

from __future__ import annotations

import json

import pytest

from aiodisklavier import (
    Disklavier,
    DisklavierCommandError,
    PlaybackSnapshot,
    SongGroup,
)
from aiodisklavier import client as client_module

from .conftest import (
    CURRENT_INFO_PAYLOAD,
    RADIO_CHANNELS,
    FakePiano,
    dumps,
    radio_channel_list,
)


def snapshot_of(
    prefix: str | None,
    song_id: int | None,
    position_ms: int = 0,
    was_playing: bool = False,
    *,
    radio_channel: str | None = None,
) -> PlaybackSnapshot:
    """Build a snapshot tersely. The model itself is keyword-only."""
    return PlaybackSnapshot(
        song_prefix=prefix,
        song_id=song_id,
        position_ms=position_ms,
        was_playing=was_playing,
        radio_active=radio_channel is not None,
        radio_channel=radio_channel,
    )


def _piano_that_obeys(fake_piano: FakePiano, *, deaf_to: int = 0) -> None:
    """Have the fake report the seeks and plays it is sent, as the piano does.

    :param deaf_to: Ignore this many seeks and plays first, the way the piano ignores
        whatever reaches it while the sequencer is still loading.
    """
    ignored = 0

    def _apply(**changes: str) -> None:
        nonlocal ignored
        if ignored < deaf_to:
            ignored += 1
            return
        fake_piano.current_body = dumps(
            {**json.loads(fake_piano.current_body), **changes}
        )

    def _on_command(command: str) -> None:
        if command == "play":
            _apply(playback_status="play")

    def _on_seek(position: str) -> None:
        _apply(playback_position=position)

    fake_piano.on_command = _on_command
    fake_piano.on_seek = _on_seek


def commands_sent(fake_piano: FakePiano) -> list[str]:
    """List the open API commands the piano received, in order, minus state reads."""
    return [
        request.command
        for request in fake_piano.requests
        if request.command not in (None, "current_info", "static_info")
    ]


# ----------------------------------------------------------------------
# Snapshot
# ----------------------------------------------------------------------


async def test_snapshot_reads_master_json(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A snapshot comes from master.json, which alone carries the library prefix."""
    snapshot = await piano.async_snapshot_playback()
    assert fake_piano.last.path == "/ctrl/master.json"
    assert snapshot.position_ms == 516000
    assert snapshot.was_playing is False


def test_snapshot_has_song() -> None:
    """A snapshot is restorable only when both prefix and id are known."""
    assert snapshot_of("y", 24, 1000, False).has_song is True
    assert snapshot_of(None, 24, 1000, False).has_song is False
    assert snapshot_of("y", None, 1000, False).has_song is False


def test_snapshot_was_playing() -> None:
    """Only 'play' counts as playing."""
    for status, expected in (("play", True), ("pause", False), ("stop", False)):
        snapshot = PlaybackSnapshot.from_master_json({"seq": {"status": status}})
        assert snapshot.was_playing is expected


# ----------------------------------------------------------------------
# Restore
# ----------------------------------------------------------------------


async def test_restore_uses_load_song_not_setsong(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Restore must not go through setSong.php.

    The firmware hardcodes ``control="play"`` in ``setSong.php?prefix=&song_id=``, so using
    it to reselect would start playback instead of quietly cueing the song.
    """
    await piano.async_restore_playback(snapshot_of("y", 24, 516000, False))

    paths = [request.path for request in fake_piano.requests]
    assert "/ctrl/setSong.php" not in paths

    commands = [
        request.command
        for request in fake_piano.requests
        if request.command is not None
    ]
    assert "load_song" in commands
    assert "play_song" not in commands


async def test_restore_stops_before_cueing(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Restore must halt whatever is sounding before selecting the previous song.

    ``load_song`` only moves the sequencer's selection; it does not stop playback. Without
    an explicit stop, restoring over a still-playing notification leaves the piano audibly
    playing one song while reporting another. Found on real hardware, not in this suite.
    """
    await piano.async_restore_playback(snapshot_of("y", 24, 516000, False))
    commands = [request.command for request in fake_piano.requests]
    assert commands.index("stop") < commands.index("load_song")


async def test_restore_maps_prefix_to_group(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The bare prefix from master.json is mapped back to an open API group."""
    await piano.async_restore_playback(snapshot_of("y", 24, 0, False))
    load = next(
        request for request in fake_piano.requests if request.command == "load_song"
    )
    assert load.query["group"] == SongGroup.DOWNLOADED_SONGS.value
    assert load.query["id"] == "24"


async def test_restore_seeks_to_position(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The saved position is restored after the song is cued."""
    await piano.async_restore_playback(snapshot_of("y", 24, 516000, False))
    seeks = [
        request for request in fake_piano.requests if request.path == "/ctrl/setSeq.php"
    ]
    assert seeks[-1].query["time"] == "516000"


async def test_restore_resumes_when_it_was_playing(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A piano that was playing is left playing."""
    _piano_that_obeys(fake_piano)
    await piano.async_restore_playback(snapshot_of("y", 24, 1000, True))
    assert commands_sent(fake_piano)[-1] == "play"
    assert commands_sent(fake_piano).count("play") == 1


async def test_restore_stays_paused_when_it_was_paused(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A piano that was paused is not started."""
    await piano.async_restore_playback(snapshot_of("y", 24, 1000, False))
    commands = [request.command for request in fake_piano.requests]
    assert "play" not in commands


async def test_restore_skips_empty_snapshot(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Nothing loaded means nothing to restore."""
    await piano.async_restore_playback(snapshot_of(None, None, 0, False))
    assert fake_piano.requests == []


async def test_restore_skips_unknown_prefix(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """An unrecognised library is skipped rather than guessed at."""
    await piano.async_restore_playback(snapshot_of("?", 24, 0, False))
    assert fake_piano.requests == []


async def test_restore_waits_for_the_song_to_load_before_seeking(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Nothing is sent to a song that is still loading.

    Found on hardware: ``load_song`` answers at once and the sequencer then loads for
    about two seconds, dropping whatever it is sent meanwhile -- HTTP 200, no effect. A
    restore that sent its seek and its ``play`` back to back put the right song up and
    left it at the start, stopped, every time. The requests went out, which is all the
    tests of the day checked.
    """
    _piano_that_obeys(fake_piano)
    statuses = iter(["load", "load", "pause"])

    def _loading(command: str) -> None:
        if command == "load_song":
            fake_piano.on_current_info = _advance
            _advance()

    def _advance() -> None:
        # Only the status moves: the seek, once sent, must still read back.
        status = next(statuses, None)
        if status is not None:
            fake_piano.current_body = dumps(
                {**json.loads(fake_piano.current_body), "playback_status": status}
            )

    obey = fake_piano.on_command
    assert obey is not None

    def _on_command(command: str) -> None:
        _loading(command)
        obey(command)

    fake_piano.on_command = _on_command

    await piano.async_restore_playback(snapshot_of("y", 24, 120000, False))

    paths = [request.command or request.path for request in fake_piano.requests]
    cued = paths.index("load_song")
    sought = paths.index("/ctrl/setSeq.php")
    # load, load, pause: three looks before the seek was allowed out.
    assert paths[cued + 1 : sought] == ["current_info"] * 3


async def test_restore_does_not_stop_a_piano_that_is_already_stopped(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The sequencer is sent as little as possible.

    Straight after a notification has been silenced the piano is stopped already, and a
    second ``stop`` a moment later is one more command for a fragile daemon to absorb.
    """
    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_position": "0"})
    await piano.async_restore_playback(snapshot_of("y", 24, 0, False))
    assert commands_sent(fake_piano) == ["load_song"]


async def test_restore_does_not_wait_when_cueing_is_all_there_is(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A song restored paused at the start needs nothing sent after ``load_song``."""
    await piano.async_restore_playback(snapshot_of("y", 24, 0, False))
    assert commands_sent(fake_piano)[-1] == "load_song"
    assert fake_piano.last.command == "load_song"


async def test_restore_gives_up_on_a_load_that_never_finishes(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sequencer stuck loading does not hold the caller for ever.

    This is what a wedged sequencer looks like from outside -- ``load`` for good, as seen
    on hardware -- and the restore has to come back from it rather than hang.
    """
    monkeypatch.setattr(client_module, "LOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "LOAD_POLL_INTERVAL", 0.01)
    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_status": "load"})

    await piano.async_restore_playback(snapshot_of("y", 24, 120000, False))

    assert "/ctrl/setSeq.php" in [request.path for request in fake_piano.requests]


async def test_restore_sends_a_dropped_seek_once_more(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The position is read back, and a seek the piano dropped is sent again -- once.

    The wait for the load is a judgement about timing on one piano; this check does not
    depend on it being right.
    """
    _piano_that_obeys(fake_piano, deaf_to=1)
    await piano.async_restore_playback(snapshot_of("y", 24, 120000, False))
    seeks = [r for r in fake_piano.requests if r.path == "/ctrl/setSeq.php"]
    assert [seek.query["time"] for seek in seeks] == ["120000", "120000"]


async def test_restore_does_not_hammer_a_piano_that_will_not_seek(
    piano: Disklavier, fake_piano: FakePiano, caplog: pytest.LogCaptureFixture
) -> None:
    """Two attempts and no more, then it moves on.

    The sequencer daemon is fragile, and a command it has ignored twice is not going to be
    obeyed a third time. Repeating into a daemon that is in trouble is how to make it
    worse -- it wedged during the hardware session that found all this, with a version
    of this loop that tried four times.
    """
    _piano_that_obeys(fake_piano, deaf_to=99)
    with caplog.at_level("DEBUG", logger="aiodisklavier.client"):
        await piano.async_restore_playback(snapshot_of("y", 24, 120000, True))
    paths = [request.command or request.path for request in fake_piano.requests]
    assert paths.count("/ctrl/setSeq.php") == 2
    assert paths.count("play") == 2
    assert "did not keep a seek" in caplog.text
    assert "did not start playing" in caplog.text


async def test_restore_accepts_a_position_within_the_pianos_resolution(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Position resolution is about a second, so a near miss is a kept seek."""
    fake_piano.current_body = dumps(
        {**CURRENT_INFO_PAYLOAD, "playback_position": "119000"}
    )
    await piano.async_restore_playback(snapshot_of("y", 24, 120000, False))
    seeks = [r for r in fake_piano.requests if r.path == "/ctrl/setSeq.php"]
    assert len(seeks) == 1


# ----------------------------------------------------------------------
# Notify
# ----------------------------------------------------------------------


async def test_notify_requires_a_target(piano: Disklavier) -> None:
    """A notification needs either a song plus group, or a search title."""
    with pytest.raises(ValueError, match="song_id"):
        await piano.async_notify()
    with pytest.raises(ValueError, match="song_id"):
        await piano.async_notify(song_id=1)


async def test_notify_plays_as_one_shot(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The notification must not run on into the rest of the library."""
    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS, restore=False)
    commands = [request.command for request in fake_piano.requests]
    assert "play_single_song" in commands


async def test_notify_by_search(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A notification can be addressed by title."""
    await piano.async_notify(search_title="Silent Night", restore=False)
    play = next(
        request
        for request in fake_piano.requests
        if request.command == "play_single_song"
    )
    assert play.query["search_title"] == "Silent Night"


async def test_notify_restores_previous_song(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """After the notification the previous song is cued back up."""
    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)
    commands = [request.command for request in fake_piano.requests]
    # The fake piano reports song 'y'/24 at 516000 in master.json.
    assert commands.index("play_single_song") < commands.index("load_song")
    seeks = [
        request for request in fake_piano.requests if request.path == "/ctrl/setSeq.php"
    ]
    assert seeks[-1].query["time"] == "516000"


async def test_notify_waits_for_the_notification_to_finish(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The restore must wait for the notification to stop sounding, not race it.

    This is the behaviour the feature exists for: once the notification starts, the fake
    reports 'play' for three polls before flipping to 'pause', and no restore command may
    go out until the piano has reported quiet.
    """
    polls = 0

    def _start(command: str) -> None:
        if command == "play_single_song":
            fake_piano.current_body = dumps(
                {**CURRENT_INFO_PAYLOAD, "playback_status": "play"}
            )
            fake_piano.on_current_info = _finish_after_three

    def _finish_after_three() -> None:
        nonlocal polls
        polls += 1
        if polls >= 3:
            # CURRENT_INFO_PAYLOAD reports 'pause'. The fake has already picked the body
            # for the poll in hand, so this lands on the next one.
            fake_piano.current_body = dumps(CURRENT_INFO_PAYLOAD)
            fake_piano.on_current_info = None

    fake_piano.on_command = _start

    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)

    paths = [request.command or request.path for request in fake_piano.requests]
    started = paths.index("play_single_song")
    silenced = paths.index("stop")
    # Three 'play' polls, then the first 'pause' poll ends the wait -- exactly four,
    # so a loop that stopped exiting promptly (and ran to its deadline instead)
    # cannot slip through on the ordering assertion alone.
    assert paths[started + 1 : silenced] == ["current_info"] * 4
    # ...and nothing was restored until after the final poll.
    assert silenced < paths.index("load_song")


async def test_notify_waits_through_the_sequencer_loading(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """'load' is the song still being fetched, not the song having finished.

    Found on hardware: on the way out of radio the piano reports ``load`` for about two
    seconds before ``play`` -- longer than the settle. Read as quiet, that ends the wait
    at once, and the restore's stop then kills the notification before its first note.
    """
    statuses = iter(["load", "load", "play", "pause"])

    def _start(command: str) -> None:
        if command == "play_single_song":
            fake_piano.on_current_info = _advance
            _advance()

    def _advance() -> None:
        status = next(statuses, "pause")
        fake_piano.current_body = dumps(
            {**CURRENT_INFO_PAYLOAD, "playback_status": status}
        )

    fake_piano.on_command = _start

    await piano.async_notify(
        song_id=1, group=SongGroup.BUILT_IN_SONGS, restore=False, volume=15
    )

    paths = [request.command for request in fake_piano.requests]
    started = paths.index("play_single_song")
    silenced = paths.index("stop")
    # load, load, play, pause: the wait saw all four, rather than leaving at the first.
    assert paths[started + 1 : silenced] == ["current_info"] * 4


async def test_notify_timeout_silences_before_restoring_volume(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the give-up path the piano is stopped before the volume comes back up.

    When the wait deadline fires, the notification is still sounding. Restoring the
    previous -- usually louder -- volume first would blast the notification's tail
    for a round-trip; the stop must land before the volume does.
    """
    monkeypatch.setattr(client_module, "NOTIFY_POLL_INTERVAL", 0.01)
    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_status": "play"})

    await piano.async_notify(
        song_id=1, group=SongGroup.BUILT_IN_SONGS, volume=15, wait_timeout=0.05
    )

    events = [
        (request.command, request.query.get("_v"))
        for request in fake_piano.requests
        if request.command in ("stop", "set_volume_main")
    ]
    # Down to the notification volume, then silence, then back up -- in that order.
    assert events[0] == ("set_volume_main", "15")
    assert events.index(("stop", None)) < events.index(("set_volume_main", "100"))


async def test_notify_failure_before_takeover_does_not_stop_playback(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A notification that never started must not silence what was already playing.

    If the play command itself is rejected, the sequencer still holds the user's own
    music. The silencing stop that protects the give-up path must not fire here: on a
    ``restore=False`` call nothing would bring that playback back.
    """
    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_status": "play"})
    fake_piano.command_status_for = {"play_single_song": 400}

    with pytest.raises(DisklavierCommandError):
        await piano.async_notify(
            song_id=1, group=SongGroup.BUILT_IN_SONGS, volume=15, restore=False
        )

    commands = [request.command for request in fake_piano.requests]
    assert "stop" not in commands
    # The volume, though, was already changed and is put back.
    volumes = [
        request.query["_v"]
        for request in fake_piano.requests
        if request.command == "set_volume_main"
    ]
    assert volumes == ["15", "100"]


async def test_notify_gives_up_waiting_and_restores_anyway(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notification that never reports finishing must not wait forever.

    The piano can sit reporting 'play' for longer than the caller wants to wait. The wait
    gives up on its deadline and restores regardless, rather than hanging.
    """
    monkeypatch.setattr(client_module, "NOTIFY_POLL_INTERVAL", 0.01)
    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_status": "play"})

    await piano.async_notify(
        song_id=1, group=SongGroup.BUILT_IN_SONGS, wait_timeout=0.05
    )

    commands = [request.command for request in fake_piano.requests]
    assert "play_single_song" in commands
    # Gave up waiting, but still put the previous selection back.
    assert "load_song" in commands


async def test_notify_restores_volume(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A notification volume is temporary."""
    await piano.async_notify(
        song_id=1, group=SongGroup.BUILT_IN_SONGS, volume=15, restore=False
    )
    volumes = [
        request.query["_v"]
        for request in fake_piano.requests
        if request.command == "set_volume_main"
    ]
    # Down to the notification volume, then back to the fake piano's reported 100.
    assert volumes == ["15", "100"]


async def test_notify_restores_even_when_playback_fails(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A failure mid-notification must not strand the piano at the wrong volume."""
    fake_piano.command_status = 400
    with pytest.raises(DisklavierCommandError):
        await piano.async_notify(
            song_id=1, group=SongGroup.BUILT_IN_SONGS, volume=15, restore=False
        )
    volumes = [
        request.query["_v"]
        for request in fake_piano.requests
        if request.command == "set_volume_main"
    ]
    assert volumes[-1] == "100"


async def test_notify_reports_the_original_failure_not_the_restore(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """When the restore also fails, the original error must still be what surfaces.

    This exercises the restore's own error handling, which a passing notification never
    reaches -- and which silently carried a NameError until this test was added.
    """
    fake_piano.command_status = 400
    with pytest.raises(DisklavierCommandError) as excinfo:
        await piano.async_notify(
            song_id=1, group=SongGroup.BUILT_IN_SONGS, volume=15, restore=True
        )
    # Not a NameError or some error raised from inside the finally block.
    assert "rejected request" in str(excinfo.value)


# ----------------------------------------------------------------------
# Radio
# ----------------------------------------------------------------------
#
# Everything here was found on hardware. While a DisklavierRadio channel is active the
# piano answers play, pause, stop, next_song and every load_* and play_* command with
# HTTP 200 and ignores them, so a notification sent over the radio was accepted and never
# sounded. Only stop_radio ends it.


async def test_notify_ends_the_radio_first(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The radio is stopped before the notification is asked to play."""
    fake_piano.start_radio()

    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS, restore=False)

    commands = commands_sent(fake_piano)
    assert commands.index("stop_radio") < commands.index("play_single_song")
    # Nothing was asked to be restored, so the radio stays off.
    assert "play_radio" not in commands


async def test_notify_leaves_the_radio_alone_when_it_is_off(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """``stop_radio`` is only sent when there is a radio to stop."""
    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)
    assert "stop_radio" not in commands_sent(fake_piano)


async def test_notify_puts_the_radio_back_on(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """After the notification, the channel that was playing is playing again.

    ``master.json`` names the channel only by title, so it is resolved through the
    channel list. The song loaded before the radio is cued again too -- it is what the
    piano falls back to when the radio ends -- but it is neither sought nor started: the
    sequencer's clock during radio belonged to the radio.
    """
    fake_piano.start_radio()

    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)

    commands = commands_sent(fake_piano)
    assert commands.index("play_single_song") < commands.index("load_song")
    assert commands[-1] == "play_radio"
    assert fake_piano.last_command("play_radio").query["channel_id"] == "1"
    assert "play" not in commands
    assert not [r for r in fake_piano.requests if r.path == "/ctrl/setSeq.php"]


async def test_notify_that_fails_after_ending_the_radio_still_restores_it(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Once the radio has been stopped for a notification, it is owed back.

    Unlike a song that was playing, there is nothing of the user's left to spare by
    holding back the silencing stop: the radio is already off, by this method's hand.
    """
    fake_piano.start_radio()
    fake_piano.command_status_for = {"play_single_song": 400}

    with pytest.raises(DisklavierCommandError):
        await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)

    commands = commands_sent(fake_piano)
    assert "stop" in commands
    assert commands[-1] == "play_radio"


async def test_restore_ends_a_radio_that_is_on_now(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Restoring a song over an active radio ends the radio first.

    Otherwise every command the restore sends is accepted and ignored, and it reports
    success having changed nothing.
    """
    fake_piano.start_radio()

    await piano.async_restore_playback(snapshot_of("y", 24, 516000, False))

    commands = commands_sent(fake_piano)
    assert commands.index("stop_radio") < commands.index("load_song")


async def test_restore_of_a_radio_with_no_song_behind_it(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A snapshot holding only a radio channel is still worth restoring."""
    await piano.async_restore_playback(snapshot_of(None, None, radio_channel="Chopin"))
    commands = commands_sent(fake_piano)
    assert "load_song" not in commands
    assert commands[-1] == "play_radio"
    assert fake_piano.last_command("play_radio").query["channel_id"] == "31"


async def test_restore_reads_the_channel_list_before_cueing(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The channel is resolved before the song is cued, not after.

    Reading the channel list resets the sequencer, so doing it afterwards would rewind
    the song the restore had just put in place.
    """
    await piano.async_restore_playback(snapshot_of("y", 24, radio_channel="Chopin"))
    commands = commands_sent(fake_piano)
    assert commands.index("get_radio_channel_list") < commands.index("load_song")


async def test_restore_skips_a_channel_that_has_left_the_lineup(
    piano: Disklavier, fake_piano: FakePiano, caplog: pytest.LogCaptureFixture
) -> None:
    """A channel that no longer exists is logged and skipped; the song is still cued."""
    with caplog.at_level("WARNING", logger="aiodisklavier.client"):
        await piano.async_restore_playback(
            snapshot_of("y", 24, radio_channel="Gone FM")
        )
    commands = commands_sent(fake_piano)
    assert "play_radio" not in commands
    assert "load_song" in commands
    assert "Gone FM" in caplog.text
    # The list was fresh, so there was no point reading it a second time.
    assert commands.count("get_radio_channel_list") == 1


async def test_restore_rereads_a_stale_lineup_once(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A miss against a cached line-up re-reads it, in case the cache predates the channel.

    Channel ids are positions in the line-up, which does change: 51 channels in August
    2026 were 50 a month later.
    """
    await piano.async_get_radio_channels()
    fake_piano.command_body_for = {
        **fake_piano.command_body_for,
        "get_radio_channel_list": radio_channel_list(
            [*RADIO_CHANNELS, {"channel_id": 51, "channel_title": "New Arrivals"}]
        ),
    }

    await piano.async_restore_playback(
        snapshot_of(None, None, radio_channel="New Arrivals")
    )

    assert commands_sent(fake_piano).count("get_radio_channel_list") == 2
    assert fake_piano.last_command("play_radio").query["channel_id"] == "51"


async def test_restore_survives_the_radio_service_declining(
    piano: Disklavier, fake_piano: FakePiano, caplog: pytest.LogCaptureFixture
) -> None:
    """If the piano will not list its channels, the song is still put back."""
    fake_piano.command_body_for = {
        "get_radio_channel_list": dumps(
            {"status": "error", "error_info": "not available"}
        )
    }
    with caplog.at_level("WARNING", logger="aiodisklavier.client"):
        await piano.async_restore_playback(snapshot_of("y", 24, radio_channel="Chopin"))
    commands = commands_sent(fake_piano)
    assert "load_song" in commands
    assert "play_radio" not in commands
    assert "not available" in caplog.text
