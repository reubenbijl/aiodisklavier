"""Fixtures for the hardware suite: the same library, pointed at a real piano.

Everything else in ``tests/`` runs against a fake that encodes what one piano did on one
firmware. That cannot notice the piano changing, and it cannot speak for anyone else's. This
suite can, and it is how each release is checked against the real thing.

**Opt-in, in three steps.** Nothing here runs unless ``DISKLAVIER_HOST`` names a piano::

    DISKLAVIER_HOST=192.168.1.50 pytest -m hardware                  # reads only
    DISKLAVIER_HOST=... DISKLAVIER_HARDWARE=changes pytest -m hardware
    DISKLAVIER_HOST=... DISKLAVIER_HARDWARE=sequencer pytest -m hardware

Run it without ``--cov``: the coverage gate is for the unit suite.

- ``read`` (the default) only reads. It is silent, changes nothing, and never asks for the
  radio channel list -- which looks like a read and stops whatever is playing.
- ``changes`` also alters settings and puts them back: the volume, the repeat mode, a
  scratch file on the share. Silent, and nothing touches the sequencer.
- ``sequencer`` also loads, seeks, plays and uses the radio. **It makes sound**, at low
  volume, and it carries a real risk: the sequencer daemon on the reference piano can wedge
  -- accept a selection and never finish loading it -- and only a reboot clears that (see
  CONTRIBUTING.md). Every test here is paced and checks the piano's health first, and the
  run stops at the first sign of trouble rather than carrying on into a daemon in distress.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import aiohttp
import pytest

from aiodisklavier import Disklavier, DisklavierShare, PlaybackStatus

#: Gaps between commands, in seconds. The firmware drops or mishandles commands that
#: arrive on top of each other, and the fake piano's zero-length waits must not leak in.
PACE = 1.5

LEVELS = ("read", "changes", "sequencer")


def _level() -> int:
    wanted = os.environ.get("DISKLAVIER_HARDWARE", "read")
    if wanted not in LEVELS:
        raise pytest.UsageError(
            f"DISKLAVIER_HARDWARE must be one of {LEVELS}, not {wanted!r}"
        )
    return LEVELS.index(wanted)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in this directory, and skip what was not asked for."""
    host = os.environ.get("DISKLAVIER_HOST")
    for item in items:
        if "tests/hardware/" not in item.nodeid.replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.hardware)
        if not host:
            item.add_marker(pytest.mark.skip(reason="set DISKLAVIER_HOST to run"))
            continue
        needs = item.get_closest_marker("needs")
        level = needs.args[0] if needs else "read"
        if LEVELS.index(level) > _level():
            item.add_marker(
                pytest.mark.skip(reason=f"set DISKLAVIER_HARDWARE={level} to run")
            )


@pytest.fixture(autouse=True)
def _no_real_time_waits() -> None:
    """Override the unit suite's fixture of the same name: a real piano needs real waits."""


@pytest.fixture
def host() -> str:
    """Name the piano under test."""
    return os.environ["DISKLAVIER_HOST"]


@pytest.fixture
async def piano(host: str) -> AsyncIterator[Disklavier]:
    """Provide a client for the real piano."""
    async with aiohttp.ClientSession() as session:
        yield Disklavier(host, session, timeout=30)


@pytest.fixture
async def share(host: str) -> AsyncIterator[DisklavierShare]:
    """Provide a client for the real piano's share."""
    async with DisklavierShare(host, timeout=60) as client:
        yield client


@pytest.fixture(autouse=True)
async def _paced() -> AsyncIterator[None]:
    """Leave the piano alone for a moment between tests."""
    yield
    if os.environ.get("DISKLAVIER_HOST"):
        await asyncio.sleep(PACE)


@pytest.fixture
async def healthy(piano: Disklavier) -> None:
    """Refuse to drive a sequencer that looks wedged, and stop the whole run if it does.

    The signature, as seen on hardware: ``load`` that never clears, or a loaded song that
    reports a length of zero. Sending such a daemon more commands helps nothing.
    """
    for _ in range(20):
        info = await piano.async_get_current_info()
        if info.playback_status is not PlaybackStatus.LOAD:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.exit(
            "The piano has reported 'load' for 10 s: it looks wedged. Stopping."
        )
    if not info.is_radio and info.song_title and not info.duration_ms:
        pytest.exit("A loaded song reports length 0: the piano looks wedged. Stopping.")
