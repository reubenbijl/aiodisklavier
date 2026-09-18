"""Checks that change a setting and put it back. Silent; nothing here touches the sequencer."""

from __future__ import annotations

import asyncio

import pytest

from aiodisklavier import (
    VOLUME_STEP,
    Disklavier,
    DisklavierShare,
    DisklavierShareNotFoundError,
    RepeatMode,
)

from .conftest import PACE

pytestmark = pytest.mark.needs("changes")

SCRATCH = "aiodisklavier-hardware-test"


async def test_volume_round_trip(piano: Disklavier) -> None:
    """Volume sets, reads back, and steps by the documented amount."""
    original = (await piano.async_get_current_info()).volume
    assert original is not None
    target = 40 if original != 40 else 50
    try:
        await piano.async_set_volume(target)
        await asyncio.sleep(PACE)
        assert (await piano.async_get_current_info()).volume == target

        await piano.async_volume_up()
        await asyncio.sleep(PACE)
        assert (await piano.async_get_current_info()).volume == target + VOLUME_STEP
    finally:
        await piano.async_set_volume(original)
    await asyncio.sleep(PACE)
    assert (await piano.async_get_current_info()).volume == original


async def test_repeat_mode_round_trip_including_the_clipped_one(
    piano: Disklavier,
) -> None:
    """The one 16-character mode reads back clipped, and is still recognised."""
    original = (await piano.async_get_master_state()).repeat
    assert original is not None
    try:
        await piano.async_set_repeat(RepeatMode.PLAYLIST_SHUFFLE)
        await asyncio.sleep(PACE)
        assert (
            await piano.async_get_master_state()
        ).repeat is RepeatMode.PLAYLIST_SHUFFLE
    finally:
        await piano.async_set_repeat(original)
    await asyncio.sleep(PACE)
    assert (await piano.async_get_master_state()).repeat is original


async def test_share_round_trip(share: DisklavierShare) -> None:
    """A scratch file goes up, comes back identical, moves, and is cleaned away.

    Nothing is reindexed, so the piano's library never sees it.
    """
    payload = b"aiodisklavier hardware test\n" * 64
    first = f"{SCRATCH}/probe.txt"
    second = f"{SCRATCH}/moved.txt"
    try:
        assert await share.async_upload_bytes(payload, first) == len(payload)
        entry = await share.async_stat(first)
        assert entry.size == len(payload)
        assert await share.async_download_bytes(first) == payload

        await share.async_rename(first, second)
        assert await share.async_exists(first) is False
        assert [e.name for e in await share.async_list(SCRATCH)] == ["moved.txt"]
    finally:
        if await share.async_exists(SCRATCH):
            await share.async_delete_tree(SCRATCH)
    with pytest.raises(DisklavierShareNotFoundError):
        await share.async_stat(SCRATCH)
