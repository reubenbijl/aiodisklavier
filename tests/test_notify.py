"""Tests for snapshot, restore, and one-shot notifications."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    Disklavier,
    DisklavierCommandError,
    PlaybackSnapshot,
    SongGroup,
)
from aiodisklavier import client as client_module
from aiodisklavier.const import PATH_CURRENT_INFO

from .conftest import CURRENT_INFO_PAYLOAD, FakePiano, dumps


@pytest.fixture(autouse=True)
def _fast_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the notification's real-time waits so tests stay fast."""
    monkeypatch.setattr(client_module, "NOTIFY_SETTLE", 0.0)
    monkeypatch.setattr(client_module, "NOTIFY_POLL_INTERVAL", 0.0)


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
    assert PlaybackSnapshot("y", 24, 1000, False).has_song is True
    assert PlaybackSnapshot(None, 24, 1000, False).has_song is False
    assert PlaybackSnapshot("y", None, 1000, False).has_song is False


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
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 516000, False))

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
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 516000, False))
    commands = [request.command for request in fake_piano.requests]
    assert commands.index("stop") < commands.index("load_song")


async def test_restore_maps_prefix_to_group(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The bare prefix from master.json is mapped back to an open API group."""
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 0, False))
    load = next(
        request for request in fake_piano.requests if request.command == "load_song"
    )
    assert load.query["group"] == SongGroup.DOWNLOADED_SONGS.value
    assert load.query["id"] == "24"


async def test_restore_seeks_to_position(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The saved position is restored after the song is cued."""
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 516000, False))
    seeks = [
        request for request in fake_piano.requests if request.path == "/ctrl/setSeq.php"
    ]
    assert seeks[-1].query["time"] == "516000"


async def test_restore_resumes_when_it_was_playing(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A piano that was playing is left playing."""
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 1000, True))
    commands = [request.command for request in fake_piano.requests]
    assert commands[-1] == "play"


async def test_restore_stays_paused_when_it_was_paused(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A piano that was paused is not started."""
    await piano.async_restore_playback(PlaybackSnapshot("y", 24, 1000, False))
    commands = [request.command for request in fake_piano.requests]
    assert "play" not in commands


async def test_restore_skips_empty_snapshot(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Nothing loaded means nothing to restore."""
    await piano.async_restore_playback(PlaybackSnapshot(None, None, 0, False))
    assert fake_piano.requests == []


async def test_restore_skips_unknown_prefix(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """An unrecognised library is skipped rather than guessed at."""
    await piano.async_restore_playback(PlaybackSnapshot("?", 24, 0, False))
    assert fake_piano.requests == []


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

    This is the behaviour the feature exists for: the fake reports 'play' for three
    polls before flipping to 'pause', and no restore command may go out until the
    piano has reported quiet.
    """
    polls = 0

    def _finish_after_three() -> None:
        nonlocal polls
        polls += 1
        if polls >= 3:
            # CURRENT_INFO_PAYLOAD reports 'pause'.
            fake_piano.current_body = dumps(CURRENT_INFO_PAYLOAD)

    fake_piano.current_body = dumps({**CURRENT_INFO_PAYLOAD, "playback_status": "play"})
    fake_piano.on_current_info = _finish_after_three

    await piano.async_notify(song_id=1, group=SongGroup.BUILT_IN_SONGS)

    # Three 'play' polls, then the first 'pause' poll ends the wait -- exactly four,
    # so a loop that stopped exiting promptly (and ran to its deadline instead)
    # cannot slip through on the ordering assertion alone.
    assert polls == 4
    # ...and nothing was restored until after the final poll.
    poll_indices = [
        index
        for index, request in enumerate(fake_piano.requests)
        if request.path == PATH_CURRENT_INFO
    ]
    load_index = next(
        index
        for index, request in enumerate(fake_piano.requests)
        if request.command == "load_song"
    )
    assert poll_indices[-1] < load_index


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
