"""Tests for ``python -m aiodisklavier``, the report another owner pastes into an issue.

Two properties matter more than the formatting. It must be safe to run -- read-only, and
never the radio channel list, which stops the music -- and it must be safe to paste, which
means shapes and vocabularies, never contents.
"""

from __future__ import annotations

import asyncio
import runpy

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from smb.base import NotConnectedError

from aiodisklavier import _probe

from .conftest import (
    CURRENT_INFO_PAYLOAD,
    MASTER_PAYLOAD,
    SONG_DB_PAYLOAD,
    STATIC_INFO_PAYLOAD,
    FakePiano,
    FakeSMBServer,
    dumps,
    ok_envelope,
)

# What a real master.json holds that must never reach a bug report. Observed on hardware:
# the account's email address sits in ``login.username``, a passcode in ``system``.
SENSITIVE_MASTER = {
    **MASTER_PAYLOAD,
    "login": {"status": "yes", "username": "someone@example.com"},
    "system": {"login_passcode": "4711", "lang": "en"},
    "msgbox": {"type": "clear", "msg": "A private note"},
}


async def _probe_fake(
    server: TestServer,
    session: aiohttp.ClientSession,
    smb_server: FakeSMBServer,
    *,
    share: bool = True,
) -> str:
    assert server.port is not None
    return await _probe.probe(
        server.host,
        session,
        port=server.port,
        share=share,
        connection_factory=smb_server.backend,
    )


async def test_probe_describes_the_piano(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """Every part of the report is there, with the vocabulary fields' values."""
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])
    smb_server.add_file("HousePianistApp/Chopin/ballade.mid", b"MThd")
    smb_server.add_file("loose.mid", b"MThd")

    report = await _probe_fake(server, session, smb_server)

    assert "enspire_version: '5.24.00'" in report
    assert "power_status: 'on' (known)" in report
    assert "status: str = 'pause'" in report  # seq.status, allow-listed
    assert "'d': 2" in report  # song prefixes, by count
    assert "'SMFXG': 1" in report
    assert "built_in_songs: songs 0, albums 0" in report
    assert "playlists: playlists 0" in report
    assert "guest session: accepted" in report
    assert "share root: 1 folders, 1 files" in report
    assert "'PC Sharing Folder'" in report


async def test_probe_prints_shapes_not_contents(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """Nothing identifying, private or personal survives into the report.

    The allow-list is the point: the state file is printed by key and type, and values
    only for the handful of fields that are a status or a mode. A deny-list would leak
    the first sensitive field a future firmware added.
    """
    fake_piano.master_body = dumps(SENSITIVE_MASTER)
    fake_piano.command_body = ok_envelope(
        song_list=[{"song_id": 7, "song_title": "A Very Private Recording"}]
    )
    smb_server.add_file("Diary/entry.mid", b"MThd")

    report = await _probe_fake(server, session, smb_server)

    for secret in (
        "someone@example.com",
        "4711",
        "A private note",
        STATIC_INFO_PAYLOAD["disklavier_id"],
        CURRENT_INFO_PAYLOAD["song_title"],
        CURRENT_INFO_PAYLOAD["song_artist"],
        "A Very Private Recording",
        "Clair de lune",  # a title in the song database
        "Diary",
        server.host,
    ):
        assert secret not in report
    # The fields are still accounted for, by type.
    assert "username: str" in report
    assert "login_passcode: str" in report
    assert "disklavier_id: present, not shown" in report


async def test_probe_never_asks_for_the_radio_channels(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """The one "read" that is not one stays out of a tool that promises to be harmless.

    ``get_radio_channel_list`` stops the sequencer: run during a song, it would cut the
    music off. Nor does anything here change state -- every request is a state file or a
    ``get_*`` listing.
    """
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])
    await _probe_fake(server, session, smb_server, share=False)

    commands = {r.command for r in fake_piano.requests if r.command is not None}
    assert "get_radio_channel_list" not in commands
    assert all(
        command.startswith("get_") or command.endswith("_info") for command in commands
    )
    assert {r.path for r in fake_piano.requests if r.command is None} == {
        "/ctrl/master.json",
        "/ctrl/song.json",
    }


async def test_probe_flags_what_this_library_does_not_recognise(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """An unknown value is the most useful line in the report, so it is made to stand out."""
    fake_piano.current_body = dumps(
        {**CURRENT_INFO_PAYLOAD, "power_status": "defragmenting", "quiet_status": ""}
    )
    fake_piano.master_body = dumps({**MASTER_PAYLOAD, "repeat": "sideways"})
    database = {
        **SONG_DB_PAYLOAD,
        "song": {
            **SONG_DB_PAYLOAD["song"],
            "d2": {"pfix": "d", "song_id": "2", "format": "SMF,FLAC"},
            "junk": "not a row",
        },
    }
    fake_piano.song_db_body = dumps(database)
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])

    report = await _probe_fake(server, session, smb_server, share=False)

    assert "power_status: 'defragmenting' (NOT RECOGNISED)" in report
    assert "quiet_status: '' (absent)" in report
    assert "repeat verdict: NOT RECOGNISED" in report
    assert (
        "'q': 1   <-- NOT RECOGNISED" in report
    )  # the fake database's unmapped prefix
    assert "'SMF,FLAC': 1   <-- NOT RECOGNISED" in report
    assert "'?': 1" in report  # a row that is not an object at all


async def test_probe_reports_a_piano_that_lacks_the_internal_endpoints(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """A missing endpoint is a finding, and must not cost the rest of the report."""
    fake_piano.ctrl_status = 404
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])

    report = await _probe_fake(server, session, smb_server, share=False)

    assert report.count("could not read: DisklavierResponseError") == 2
    assert "enspire_model: 'PRO'" in report
    assert "built_in_songs: songs 0" in report
    assert "## SMB share" not in report


async def test_probe_reports_listings_the_piano_declines_or_rejects(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """A listing that fails says how, in place of its count."""
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])
    fake_piano.command_body_for = {
        "get_album_list": dumps({"status": "error", "error_info": "not supported"})
    }
    fake_piano.command_status_for = {"get_playlist_list": 400}

    report = await _probe_fake(server, session, smb_server, share=False)

    assert "albums declined ('not supported')" in report
    assert "playlists failed (DisklavierCommandError)" in report


async def test_probe_reports_a_share_it_cannot_reach(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """No SMB is a finding too: a model without the share, or a firewalled port."""
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])
    smb_server.connect_error = NotConnectedError("refused")

    report = await _probe_fake(server, session, smb_server)

    assert "## SMB share\n- could not read: DisklavierConnectionError" in report


async def test_probe_reports_an_identity_with_pieces_missing(
    server: TestServer,
    session: aiohttp.ClientSession,
    fake_piano: FakePiano,
    smb_server: FakeSMBServer,
) -> None:
    """Fields another firmware drops or adds are named, since that is the news."""
    fake_piano.static_body = dumps({"enspire_model": "ST", "enspire_colour": "white"})
    fake_piano.command_body = ok_envelope(song_list=[], album_list=[], playlist_list=[])

    report = await _probe_fake(server, session, smb_server, share=False)

    assert "disklavier_id: ABSENT" in report
    assert "'piano_type'" in report  # listed as missing
    assert "fields this library does not read: ['enspire_colour']" in report


def test_shape_stops_at_a_fixed_depth_and_clips_long_values() -> None:
    """A device cannot make the report arbitrarily deep, or a value arbitrarily long."""
    lines: list[str] = []
    nested = {"a": {"b": {"c": {"d": "too deep"}}}}
    _probe._shape(nested, "master.seq", 0, lines)
    assert lines[-1].strip() == "- c: dict"

    lines.clear()
    _probe._shape("x" * 500, "master.repeat", 0, lines)
    assert len(lines[0]) < 80
    assert lines[0].endswith("...'")


async def test_main_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line hands its arguments through and prints what comes back.

    ``main`` calls ``asyncio.run``, so it is run in a worker thread: there it owns its
    event loop outright, as it does on a real command line, instead of fighting the one
    this test is already running on.
    """
    seen: dict[str, object] = {}

    async def _fake_probe(host: str, session: object, **kwargs: object) -> str:
        seen.update(kwargs, host=host)
        return "the report\n"

    monkeypatch.setattr(_probe, "probe", _fake_probe)

    status = await asyncio.to_thread(
        _probe.main, ["piano.local", "--port", "8080", "--no-share"]
    )

    assert status == 0
    assert capsys.readouterr().out == "the report\n"
    assert seen == {
        "host": "piano.local",
        "port": 8080,
        "timeout": 10.0,
        "share": False,
    }


def test_running_the_package_as_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m aiodisklavier`` reaches the probe and exits with its status."""
    monkeypatch.setattr(_probe, "main", lambda: 0)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("aiodisklavier", run_name="__main__")
    assert excinfo.value.code == 0
