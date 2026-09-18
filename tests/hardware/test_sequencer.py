"""Checks that drive the sequencer and the radio. **Audible**, at low volume, and paced.

Read the warning in ``conftest.py`` before turning these on. Every test takes the ``healthy``
fixture, which stops the whole run if the piano looks wedged, and puts back the song, the
position and the volume it found.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from aiodisklavier import Disklavier, PlaybackSnapshot, SongGroup

from .conftest import PACE

pytestmark = [pytest.mark.needs("sequencer"), pytest.mark.usefixtures("healthy")]

#: Loud enough to hear that it worked, quiet enough not to startle anyone.
TEST_VOLUME = 15


@pytest.fixture
async def put_back(piano: Disklavier) -> AsyncIterator[PlaybackSnapshot]:
    """Hand the test what was there before, and restore it -- and the volume -- after."""
    snapshot = await piano.async_snapshot_playback()
    volume = (await piano.async_get_current_info()).volume
    await piano.async_set_volume(TEST_VOLUME)
    await asyncio.sleep(PACE)
    try:
        yield snapshot
    finally:
        await asyncio.sleep(PACE)
        if (await piano.async_get_current_info()).is_radio:
            await piano.async_stop_radio()
            await asyncio.sleep(PACE)
        await piano.async_restore_playback(snapshot)
        await asyncio.sleep(PACE)
        if volume is not None:
            await piano.async_set_volume(volume)


async def _first_built_in_song(piano: Disklavier) -> int:
    songs = await piano.async_get_songs(SongGroup.BUILT_IN_SONGS)
    if not songs:
        pytest.skip("the built-in library is empty")
    return songs[0].song_id


async def test_the_test_chord_leaves_the_sequencer_alone(piano: Disklavier) -> None:
    """The chord sounds through the MIDI daemon; what is loaded does not move."""
    before = await piano.async_snapshot_playback()
    await piano.async_play_test_chord()
    await asyncio.sleep(PACE)
    assert await piano.async_snapshot_playback() == before


async def test_restore_brings_back_a_paused_position(
    piano: Disklavier, put_back: PlaybackSnapshot
) -> None:
    """Cue one song, then restore another at a position: the position must be kept.

    This is the check the unit suite cannot make. For two releases the restore sent its
    seek while the song was still loading, the piano dropped it, and every test passed.
    """
    song_id = await _first_built_in_song(piano)
    await piano.async_stop()
    await asyncio.sleep(PACE)
    await piano.async_play_song(song_id, SongGroup.BUILT_IN_SONGS, load_only=True)
    await asyncio.sleep(4)

    target = PlaybackSnapshot(
        song_prefix="d", song_id=song_id, position_ms=20000, was_playing=False
    )
    await piano.async_restore_playback(target)
    await asyncio.sleep(PACE)

    info = await piano.async_get_current_info()
    assert info.is_playing is False
    assert info.position_ms is not None
    assert abs(info.position_ms - 20000) <= 1500


async def test_restore_resumes_a_song_that_was_playing(
    piano: Disklavier, put_back: PlaybackSnapshot
) -> None:
    """A snapshot taken mid-song comes back playing, from where it was."""
    song_id = await _first_built_in_song(piano)
    target = PlaybackSnapshot(
        song_prefix="d", song_id=song_id, position_ms=20000, was_playing=True
    )
    await piano.async_restore_playback(target)
    await asyncio.sleep(3)

    info = await piano.async_get_current_info()
    assert info.is_playing is True
    assert info.position_ms is not None
    assert info.position_ms >= 20000
    await piano.async_stop()


async def test_a_notification_plays_and_the_piano_is_put_back(
    piano: Disklavier, put_back: PlaybackSnapshot
) -> None:
    """The notification sounds, stops at its timeout, and what was there returns."""
    song_id = await _first_built_in_song(piano)
    await piano.async_notify(
        song_id=song_id,
        group=SongGroup.BUILT_IN_SONGS,
        volume=TEST_VOLUME,
        wait_timeout=4,
    )
    await asyncio.sleep(PACE)

    after = await piano.async_snapshot_playback()
    assert (after.song_prefix, after.song_id) == (
        put_back.song_prefix,
        put_back.song_id,
    )
    assert abs(after.position_ms - put_back.position_ms) <= 1500


async def test_radio_is_a_mode_that_ignores_the_transport(
    piano: Disklavier, put_back: PlaybackSnapshot
) -> None:
    """Radio reports itself, names its programme, and shrugs off ``pause``.

    Needs a DisklavierRadio channel the account can play; channel 1 is the free sampler.
    """
    await piano.async_play_radio(1)
    info = await piano.async_get_current_info()
    assert info.is_radio is True
    assert info.song_title is None
    master = await piano.async_get_master_state()
    assert master.radio_channel is not None

    await asyncio.sleep(PACE)
    await piano.async_pause()
    await asyncio.sleep(PACE)
    assert (await piano.async_get_current_info()).is_radio is True

    await piano.async_stop_radio()
    info = await piano.async_get_current_info()
    assert info.is_radio is False
