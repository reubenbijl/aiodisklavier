"""Tests for the SMB share client.

These run against :class:`~tests.conftest.FakeSMBServer` rather than a live share. What
that fake answers -- the NT status codes in particular -- was taken from the piano itself,
so the behaviour under test is the piano's, even though the socket is not.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import IO, Any

import pytest
from nmb.nmb_structs import NMBError
from nmb.nmb_structs import NotConnectedError as NMBNotConnectedError
from smb.base import NotConnectedError, NotReadyError, SMBTimeout
from smb.smb_structs import OperationFailure, SMBMessage

from aiodisklavier import (
    DisklavierConnectionError,
    DisklavierShare,
    DisklavierShareAuthError,
    DisklavierShareError,
    DisklavierShareExistsError,
    DisklavierShareNotFoundError,
    ShareEntry,
    SyncAction,
    SyncProgress,
)
from aiodisklavier.const import PLAYABLE_SUFFIXES
from aiodisklavier.share import (
    SyncResult,
    _clean,
    _join,
    _matches,
    _needs_upload,
    _parents,
    _timestamp,
    _wire,
)

from .conftest import (
    STATUS_ACCESS_DENIED,
    STATUS_NAME_NOT_FOUND,
    FakeSMBConnection,
    shared_file,
    smb_failure,
)

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", ""),
        (".", ""),
        ("/", ""),
        ("a.mid", "a.mid"),
        ("/ImpromptuApp/a.mid", "ImpromptuApp/a.mid"),
        ("ImpromptuApp//a.mid", "ImpromptuApp/a.mid"),
        ("ImpromptuApp/./a.mid", "ImpromptuApp/a.mid"),
        # Either separator, because callers arrive from both worlds.
        ("\\ImpromptuApp\\a.mid", "ImpromptuApp/a.mid"),
    ],
)
def test_clean_normalises_paths(given, expected):
    """Paths reduce to a share-relative POSIX form, with the root as the empty string."""
    assert _clean(given) == expected


@pytest.mark.parametrize("given", ["..", "../etc", "a/../../b", "a\\..\\..\\b"])
def test_clean_refuses_traversal(given):
    """A path that climbs out of the share is refused before it reaches the wire."""
    with pytest.raises(ValueError, match="stay inside the share"):
        _clean(given)


@pytest.mark.parametrize("given", ["a\x00b", "a\nb", "a\x7fb"])
def test_clean_refuses_control_characters(given):
    """Control characters cannot be part of a real name, and can confuse a server."""
    with pytest.raises(ValueError, match="control character"):
        _clean(given)


def test_wire_and_join_and_parents():
    """The small path helpers agree on where the root is."""
    assert _wire("") == "/"
    assert _wire("a/b") == "/a/b"
    assert _join("", "a") == "a"
    assert _join("a", "b", "c") == "a/b/c"
    assert _parents("a/b/c.mid") == ["a", "a/b"]
    assert _parents("c.mid") == []


def test_matches_ignores_case_regardless_of_platform():
    """Exclusion behaves the same on a case-sensitive filesystem and a folded one."""
    assert _matches(".DS_Store", [".ds_store"])
    assert _matches("._Song.mid", ["._*"])
    assert not _matches("song.mid", ["._*"])


def test_timestamp_falls_back_for_nonsense():
    """A server reporting an impossible time degrades rather than breaking the listing."""
    assert _timestamp(0) == datetime.fromtimestamp(0, tz=UTC)
    assert _timestamp(1e300) == datetime.fromtimestamp(0, tz=UTC)


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


def test_sync_result_reports_whether_anything_moved():
    """``changed`` is what decides whether a reindex is worth issuing."""
    assert not SyncResult(skipped=("a.mid",)).changed
    assert SyncResult(uploaded=("a.mid",)).changed
    assert SyncResult(removed=("a.mid",)).changed
    assert SyncResult(directories=("dir",)).changed


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------


async def test_connects_once_and_reuses_the_session(share, smb_server):
    """A long-lived client opens one session, however many operations run through it."""
    assert not share.connected
    await share.async_connect()
    assert share.connected
    await share.async_list()
    await share.async_list()
    assert smb_server.connects == 1


async def test_context_manager_opens_and_closes(smb_server):
    """The async context manager is the tidy way to scope a session."""
    async with DisklavierShare(
        "piano.local", connection_factory=smb_server.backend
    ) as opened:
        assert opened.connected
        assert opened.host == "piano.local"
        assert opened.share == "PC Sharing Folder"
    assert smb_server.connects == 1
    assert smb_server.closes == 1


async def test_close_is_safe_when_nothing_is_open(share, smb_server):
    """Closing an unopened client is a no-op, not an error."""
    await share.async_close()
    assert smb_server.closes == 0


async def test_rejected_credentials_raise_auth_error(smb_server):
    """A share that refuses the login says so as an auth error, not a connection one."""
    smb_server.connect_result = False
    client = DisklavierShare(
        "piano.local", username="nobody", connection_factory=smb_server.backend
    )
    with pytest.raises(DisklavierShareAuthError, match="rejected the SMB credentials"):
        await client.async_connect()
    assert not client.connected


async def test_unreachable_share_raises_connection_error(smb_server):
    """A socket that will not open is a connection failure."""
    smb_server.connect_error = OSError("no route to host")
    client = DisklavierShare("piano.local", connection_factory=smb_server.backend)
    with pytest.raises(DisklavierConnectionError, match="Could not reach"):
        await client.async_connect()


@pytest.mark.parametrize(
    "error",
    [NotReadyError("not authenticated"), smb_failure("refused", STATUS_ACCESS_DENIED)],
)
async def test_refused_session_raises_auth_error(smb_server, error):
    """A server that answers the negotiate but refuses the session is an auth failure."""
    smb_server.connect_error = error
    client = DisklavierShare("piano.local", connection_factory=smb_server.backend)
    with pytest.raises(DisklavierShareAuthError, match="refused the SMB session"):
        await client.async_connect()


async def test_failure_to_close_a_discarded_session_is_ignored(smb_server):
    """Cleanup must not replace the error that led to the cleanup."""

    class Unclosable(FakeSMBConnection):
        """A connection whose socket has already gone."""

        def close(self) -> None:
            """Fail the way a dead socket does."""
            raise OSError("socket already gone")

    smb_server.connect_result = False
    client = DisklavierShare(
        "piano.local", connection_factory=lambda: Unclosable(smb_server)
    )
    with pytest.raises(DisklavierShareAuthError):
        await client.async_connect()


async def test_default_factory_builds_a_pysmb_connection():
    """Left to itself, the client builds the real thing rather than nothing at all."""
    from smb.SMBConnection import SMBConnection

    client = DisklavierShare("piano.local", port=139, direct_tcp=False)
    assert isinstance(client._default_factory(), SMBConnection)


# ----------------------------------------------------------------------
# Losing the session
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        NotConnectedError("session gone"),
        SMBTimeout("timed out"),
        ConnectionResetError("reset by peer"),
        # The NetBIOS framing layer beneath SMB, which has its own exception tree: NMBError
        # derives from Exception, and nmb's NotConnectedError is a different class from
        # smb.base's. Both escaped an earlier version of this, partway through a large
        # transfer, and reached the caller as a bare NMBError.
        NMBError("Invalid protocol header for Direct TCP session message"),
        NMBNotConnectedError("netbios session closed"),
    ],
)
async def test_a_dropped_session_reconnects_once(share, smb_server, error):
    """The piano drops idle sessions; the first casualty should not reach the caller."""
    smb_server.add_file("a.mid", b"data")
    smb_server.fail_next = [error]

    entries = await share.async_list()

    assert [entry.name for entry in entries] == ["a.mid"]
    assert smb_server.connects == 2


async def test_a_session_that_stays_down_surfaces(share, smb_server):
    """One reconnect, not an endless supply."""
    smb_server.fail_next = [NotConnectedError("gone"), NotConnectedError("still gone")]

    with pytest.raises(DisklavierConnectionError, match="Lost the SMB session"):
        await share.async_list()

    assert smb_server.connects == 2


async def test_the_configured_timeout_reaches_every_operation(smb_server, tmp_path):
    """A timeout that only governs the handshake is a timeout in name only.

    Every ``pysmb`` method takes its own, defaulting to 30 s. Leaving them off would let a
    caller ask for `timeout=120` to cover slow transfers and quietly get 30.
    """
    source = tmp_path / "a.mid"
    source.write_bytes(b"MThd")
    client = DisklavierShare(
        "piano.local", timeout=120, connection_factory=smb_server.backend
    )

    await client.async_list_shares()
    await client.async_makedirs("dir")
    await client.async_upload(source, "dir/a.mid")
    await client.async_download_bytes("dir/a.mid")
    await client.async_list("dir")
    await client.async_stat("dir/a.mid")
    await client.async_rename("dir/a.mid", "dir/b.mid")
    await client.async_delete("dir/b.mid")
    await client.async_remove_directory("dir")
    await client.async_close()

    assert smb_server.timeouts, "no operation recorded a timeout"
    assert set(smb_server.timeouts) == {120}


async def test_a_cancelled_operation_does_not_leave_the_session_shared(smb_server):
    """A cancelled await abandons the session rather than handing it to the next caller.

    ``asyncio.to_thread`` cannot cancel the worker, so it is still mid-request on the
    socket when the lock is released. Reusing that connection puts two writers on one SMB
    stream, which desynchronises it and surfaces later as a framing error somewhere else
    entirely.
    """
    import threading

    started, release = threading.Event(), threading.Event()
    original = smb_server.store_file

    def blocking_store(path: str, handle: IO[bytes]) -> int:
        started.set()
        release.wait(5)
        return int(original(path, handle))

    smb_server.store_file = blocking_store
    client = DisklavierShare("piano.local", connection_factory=smb_server.backend)

    task = asyncio.create_task(
        client.async_upload_bytes(b"x", "big.bin", make_parents=False)
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not client.connected
    release.set()
    smb_server.store_file = original
    await client.async_upload_bytes(b"y", "next.bin", make_parents=False)
    # A second session, so the abandoned one is not being written to by two callers.
    assert smb_server.connects == 2
    await client.async_close()


async def test_a_refused_request_is_not_retried(share, smb_server):
    """The server answering "no" is an answer. Asking again would only hear it again."""
    smb_server.fail_next = [NotReadyError("not authenticated")]

    with pytest.raises(DisklavierShareAuthError, match="not authenticated"):
        await share.async_list()

    assert smb_server.connects == 1


# ----------------------------------------------------------------------
# Error translation
# ----------------------------------------------------------------------


async def test_missing_path_raises_not_found(share):
    """A missing leaf is distinguishable from every other failure, and carries its path."""
    with pytest.raises(DisklavierShareNotFoundError) as caught:
        await share.async_stat("nope.mid")

    assert caught.value.path == "nope.mid"
    assert caught.value.status == STATUS_NAME_NOT_FOUND


async def test_denied_operation_raises_auth_error(share, smb_server):
    """A refusal to write is an auth problem, whatever the operation was."""
    smb_server.fail_on[("store", "a.mid")] = smb_failure("denied", STATUS_ACCESS_DENIED)

    with pytest.raises(DisklavierShareAuthError, match="Share refused upload"):
        await share.async_upload_bytes(b"x", "a.mid")


async def test_unrecognised_failure_keeps_its_detail(share, smb_server):
    """An unmapped status still reaches the caller as a share error, message intact."""
    smb_server.fail_on[("store", "a.mid")] = smb_failure("oh dear", 0xC000DEAD)

    with pytest.raises(DisklavierShareError, match="oh dear") as caught:
        await share.async_upload_bytes(b"x", "a.mid")

    assert caught.value.status == 0xC000DEAD


async def test_failure_without_a_status_still_translates(share, smb_server):
    """``pysmb`` can raise with no status attached; that must not crash the mapping."""
    smb_server.fail_on[("store", "a.mid")] = OperationFailure("bare failure", [])

    with pytest.raises(DisklavierShareError, match="bare failure") as caught:
        await share.async_upload_bytes(b"x", "a.mid")

    assert caught.value.status is None


async def test_status_is_read_past_the_clean_messages(share, smb_server):
    """The exchange leading to a failure is in there too, and only the fault matters."""
    failure = OperationFailure("denied", [])
    clean = SMBMessage()
    faulty = SMBMessage()
    faulty.status.internal_value = STATUS_ACCESS_DENIED
    failure.smb_messages = [clean, faulty]
    smb_server.fail_on[("store", "a.mid")] = failure

    with pytest.raises(DisklavierShareAuthError) as caught:
        await share.async_upload_bytes(b"x", "a.mid")

    assert caught.value.status == STATUS_ACCESS_DENIED


async def test_failure_message_does_not_carry_the_packet_dump(share, smb_server):
    """``str(OperationFailure)`` is pages of hex. None of it belongs in a log line."""
    smb_server.fail_on[("store", "a.mid")] = smb_failure("short reason", 0xC000DEAD)

    with pytest.raises(DisklavierShareError) as caught:
        await share.async_upload_bytes(b"x", "a.mid")

    assert "SMB Message" not in str(caught.value)
    assert len(str(caught.value)) < 200


# ----------------------------------------------------------------------
# Browsing
# ----------------------------------------------------------------------


async def test_list_shares(share):
    """Listing shares is how a config flow finds out what it may connect to."""
    assert await share.async_list_shares() == ["PC Sharing Folder", "IPC$"]


async def test_list_hides_dot_entries_and_sorts_directories_first(share, smb_server):
    """A listing is presented in the order a browser wants to show it."""
    smb_server.add_file("b.mid", b"b")
    smb_server.add_file("A.mid", b"a")
    smb_server.add_directory("zeta")

    entries = await share.async_list()

    assert [entry.name for entry in entries] == ["zeta", "A.mid", "b.mid"]
    assert entries[0].is_directory
    assert entries[1].size == 1


async def test_list_reports_paths_relative_to_the_share_root(share, smb_server):
    """Every entry carries a path that can be handed straight back to another call."""
    smb_server.add_file("ImpromptuApp/maestro/x.mid", b"x")

    entries = await share.async_list("ImpromptuApp/maestro")

    assert entries[0].path == "ImpromptuApp/maestro/x.mid"
    assert await share.async_stat(entries[0].path) == entries[0]


async def test_list_drops_names_a_share_cannot_hold(share, smb_server, caplog):
    """A listing row is device-supplied data, and its name becomes a path callers join.

    SMB cannot express a separator inside a filename, so a row carrying one is not a
    well-behaved server. Passed on, ``dir/../../../etc/passwd`` escapes the moment a caller
    writes ``Path(local_dir) / entry.path``.
    """
    smb_server.add_file("dir/ok.mid", b"x")
    honest = smb_server.list_path

    def poisoned(path: str) -> list[Any]:
        rows: list[Any] = list(honest(path))
        if path == "dir":
            rows.append(
                shared_file("../../../etc/passwd", size=1, directory=False, mtime=0)
            )
            rows.append(shared_file("sub/nested", size=1, directory=False, mtime=0))
            rows.append(shared_file("back\\slash", size=1, directory=False, mtime=0))
        return rows

    smb_server.list_path = poisoned
    with caplog.at_level("WARNING"):
        entries = await share.async_list("dir")

    assert [entry.name for entry in entries] == ["ok.mid"]
    assert all(".." not in entry.path for entry in entries)
    assert caplog.text.count("is not a filename a share can hold") == 3


async def test_walk_descends(share, smb_server):
    """A walk reaches everything below the starting point."""
    smb_server.add_file("top/a.mid", b"a")
    smb_server.add_file("top/mid/b.mid", b"b")
    smb_server.add_file("top/mid/deep/c.mid", b"c")

    found = {entry.path for entry in await share.async_walk("top")}

    assert found == {
        "top/a.mid",
        "top/mid",
        "top/mid/b.mid",
        "top/mid/deep",
        "top/mid/deep/c.mid",
    }


async def test_stat_names_the_root(share):
    """The share root stats without a name, and reports itself as a directory."""
    entry = await share.async_stat("")

    assert entry == ShareEntry(
        name="", path="", is_directory=True, size=0, modified=entry.modified
    )


async def test_exists(share, smb_server):
    """``exists`` answers rather than raising, which is the point of having it."""
    smb_server.add_file("a.mid", b"a")

    assert await share.async_exists("a.mid")
    assert not await share.async_exists("b.mid")


async def test_exists_does_not_swallow_a_real_failure(share, smb_server):
    """Only "not there" is an answer. Being refused the question is still an error."""
    smb_server.fail_on[("stat", "a.mid")] = smb_failure("denied", STATUS_ACCESS_DENIED)

    with pytest.raises(DisklavierShareAuthError):
        await share.async_exists("a.mid")


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


async def test_makedirs_creates_the_whole_chain(share, smb_server):
    """Missing parents are created, shallowest first."""
    created = await share.async_makedirs("ImpromptuApp/maestro/Chopin")

    assert created == [
        "ImpromptuApp",
        "ImpromptuApp/maestro",
        "ImpromptuApp/maestro/Chopin",
    ]
    assert "ImpromptuApp/maestro/Chopin" in smb_server.paths


async def test_makedirs_is_idempotent(share):
    """Running it twice is not an error, and reports that it did nothing."""
    await share.async_makedirs("a/b")

    assert await share.async_makedirs("a/b") == []


async def test_makedirs_does_not_swallow_a_real_failure(share, smb_server):
    """Only a name collision means "already done". A refusal is still a refusal."""
    smb_server.fail_on[("mkdir", "a")] = smb_failure("denied", STATUS_ACCESS_DENIED)

    with pytest.raises(DisklavierShareAuthError):
        await share.async_makedirs("a/b")


async def test_upload_creates_parents_by_default(share, smb_server, tmp_path):
    """A caller naming a nested destination should not have to prepare the way."""
    source = tmp_path / "song.mid"
    source.write_bytes(b"MThd-pretend")

    written = await share.async_upload(source, "ImpromptuApp/songs/song.mid")

    assert written == len(b"MThd-pretend")
    assert smb_server.nodes["ImpromptuApp/songs/song.mid"].data == b"MThd-pretend"


async def test_upload_can_skip_parent_creation(share, smb_server, tmp_path):
    """The sync path already knows the directories exist, and should not ask again."""
    source = tmp_path / "song.mid"
    source.write_bytes(b"x")

    with pytest.raises(DisklavierShareNotFoundError):
        await share.async_upload(source, "missing/song.mid", make_parents=False)

    assert ("mkdir", "missing") not in smb_server.calls


async def test_upload_bytes(share, smb_server):
    """Content already in memory does not need a temporary file to get onto the share."""
    assert await share.async_upload_bytes(b"hello", "a.txt") == 5
    assert smb_server.nodes["a.txt"].data == b"hello"


async def test_upload_bytes_can_skip_parent_creation(share, smb_server):
    """The same escape hatch as the file version, for a caller that has already prepared."""
    with pytest.raises(DisklavierShareNotFoundError):
        await share.async_upload_bytes(b"x", "missing/a.txt", make_parents=False)

    assert ("mkdir", "missing") not in smb_server.calls


async def test_upload_rewinds_when_the_session_drops(share, smb_server, tmp_path):
    """A retry must resend the whole file, not the tail of a half-consumed handle."""
    source = tmp_path / "song.mid"
    source.write_bytes(b"0123456789")
    smb_server.fail_next = [NotConnectedError("gone")]

    await share.async_upload(source, "song.mid")

    assert smb_server.nodes["song.mid"].data == b"0123456789"


async def test_download_writes_the_file(share, smb_server, tmp_path):
    """A download lands the whole file at the requested path."""
    smb_server.add_file("a.mid", b"contents")
    destination = tmp_path / "nested" / "a.mid"

    assert await share.async_download("a.mid", destination) == 8
    assert destination.read_bytes() == b"contents"


async def test_a_failed_download_leaves_nothing_behind(share, smb_server, tmp_path):
    """Half a file at the destination is worse than no file at all."""
    smb_server.add_file("a.mid", b"contents")
    smb_server.fail_on[("retrieve", "a.mid")] = smb_failure(
        "denied", STATUS_ACCESS_DENIED
    )
    destination = tmp_path / "a.mid"

    with pytest.raises(DisklavierShareAuthError):
        await share.async_download("a.mid", destination)

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_download_bytes(share, smb_server):
    """Reading into memory is available for the small files that suit it."""
    smb_server.add_file("a.mid", b"contents")

    assert await share.async_download_bytes("a.mid") == b"contents"


async def test_delete(share, smb_server):
    """Deleting removes the file, and says so when there is nothing to remove."""
    smb_server.add_file("a.mid", b"a")

    await share.async_delete("a.mid")

    assert "a.mid" not in smb_server.paths
    with pytest.raises(DisklavierShareNotFoundError):
        await share.async_delete("a.mid")


async def test_remove_directory_refuses_a_populated_one(share, smb_server):
    """Removing a directory does not quietly take its contents with it."""
    smb_server.add_file("dir/a.mid", b"a")

    with pytest.raises(DisklavierShareExistsError):
        await share.async_remove_directory("dir")

    assert "dir/a.mid" in smb_server.paths


async def test_delete_tree_removes_depth_first(share, smb_server):
    """Contents go before the directories holding them, or the removals would fail."""
    smb_server.add_file("top/a.mid", b"a")
    smb_server.add_file("top/deep/b.mid", b"b")

    removed = await share.async_delete_tree("top")

    assert removed[-1] == "top"
    assert removed.index("top/deep/b.mid") < removed.index("top/deep")
    assert smb_server.paths == set()


async def test_delete_tree_refuses_the_share_root(share):
    """Emptying an entire share is never what a blank argument was meant to ask for."""
    with pytest.raises(ValueError, match="share root"):
        await share.async_delete_tree("")


async def test_rename(share, smb_server):
    """Renaming moves the node, keeping its contents."""
    smb_server.add_file("a.mid", b"a")
    smb_server.add_directory("dir")

    await share.async_rename("a.mid", "dir/b.mid")

    assert smb_server.nodes["dir/b.mid"].data == b"a"
    assert "a.mid" not in smb_server.paths


# ----------------------------------------------------------------------
# Syncing
# ----------------------------------------------------------------------


@pytest.fixture
def catalogue(tmp_path):
    """Build a local tree shaped like the real catalogue, hazards included."""
    root = tmp_path / "catalogue"
    (root / "Chopin").mkdir(parents=True)
    (root / "Chopin" / "Ballade.mid").write_bytes(b"MThd-ballade")
    (root / "Chopin" / "Nocturne.mid").write_bytes(b"MThd-nocturne")
    (root / "Chopin" / "Nocturne.wav").write_bytes(b"RIFF-audio")
    # What macOS leaves lying around, and what the firmware then indexes as a song.
    (root / "Chopin" / "._Ballade.mid").write_bytes(b"applejunk")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.mid").write_bytes(b"cache")
    return root


async def test_sync_uploads_the_tree(share, smb_server, catalogue):
    """A first sync creates the directories and sends the files."""
    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert set(result.uploaded) == {
        "ImpromptuApp/Chopin/Ballade.mid",
        "ImpromptuApp/Chopin/Nocturne.mid",
        "ImpromptuApp/Chopin/Nocturne.wav",
    }
    assert result.directories == ("ImpromptuApp", "ImpromptuApp/Chopin")
    assert result.bytes_uploaded == 35
    assert result.changed


async def test_sync_keeps_apple_double_stubs_off_the_share(share, catalogue):
    """The firmware indexes these as songs and resets the piano when one is loaded."""
    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert not any("._" in path for path in result.uploaded)
    assert not any(".DS_Store" in path for path in result.uploaded)
    assert not any("__pycache__" in path for path in result.uploaded)


async def test_sync_keeps_backing_tracks_with_their_midi(share, catalogue):
    """``Nocturne.wav`` beside ``Nocturne.mid`` is that song's audio, not a stray render.

    The piano pairs them and plays the audio through the speakers while the keys play the
    MIDI, so the default playable set has to carry both or the song arrives half-missing.
    """
    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp", suffixes=PLAYABLE_SUFFIXES
    )

    assert set(result.uploaded) == {
        "ImpromptuApp/Chopin/Ballade.mid",
        "ImpromptuApp/Chopin/Nocturne.mid",
        "ImpromptuApp/Chopin/Nocturne.wav",
    }


async def test_sync_can_filter_to_midi_alone(share, catalogue):
    """Narrowing the filter still works -- it just costs you the backing tracks."""
    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp", suffixes={".mid"}
    )

    assert set(result.uploaded) == {
        "ImpromptuApp/Chopin/Ballade.mid",
        "ImpromptuApp/Chopin/Nocturne.mid",
    }


async def test_sync_takes_a_custom_filter(share, catalogue):
    """``include`` is the escape hatch for anything the other filters cannot express."""
    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp", include=lambda path: "Ballade" in path
    )

    assert result.uploaded == ("ImpromptuApp/Chopin/Ballade.mid",)


async def test_sync_is_idempotent(share, catalogue):
    """Running it again transfers nothing, which is what makes it cheap to repeat."""
    await share.async_sync_directory(catalogue, "ImpromptuApp")

    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert result.uploaded == ()
    assert result.directories == ()
    assert len(result.skipped) == 3
    assert not result.changed


async def test_sync_resends_a_changed_file(share, catalogue):
    """Size is the cheap signal, and it is checked."""
    await share.async_sync_directory(catalogue, "ImpromptuApp")
    (catalogue / "Chopin" / "Ballade.mid").write_bytes(b"MThd-ballade-revised")

    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert result.uploaded == ("ImpromptuApp/Chopin/Ballade.mid",)


async def test_sync_resends_a_touched_file_of_the_same_size(
    share, smb_server, catalogue
):
    """A same-size edit is caught by the timestamp, which the size alone would miss."""
    await share.async_sync_directory(catalogue, "ImpromptuApp")
    ballade = catalogue / "Chopin" / "Ballade.mid"
    ballade.write_bytes(b"MThd-BALLADE")
    # The share stores when the copy was written, so "local is newer" means "changed
    # since we sent it". Push the local file well past the server's clock.
    import os

    os.utime(ballade, (smb_server.now + 3600, smb_server.now + 3600))

    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert result.uploaded == ("ImpromptuApp/Chopin/Ballade.mid",)


async def test_sync_reports_progress(share, catalogue):
    """The callback is what a UI hangs a progress bar on, so its counting must be right."""
    steps: list[SyncProgress] = []

    await share.async_sync_directory(
        catalogue, "ImpromptuApp", suffixes={".mid"}, progress=steps.append
    )

    assert [step.index for step in steps] == [1, 2, 3, 4]
    assert {step.total for step in steps} == {4}
    assert [step.action for step in steps[:2]] == [SyncAction.CREATE_DIRECTORY] * 2
    assert steps[2].action is SyncAction.UPLOAD
    assert steps[2].size == 12


async def test_sync_reports_directories_it_did_not_create(share, smb_server, catalogue):
    """A destination under an existing directory must not claim to have created it.

    Otherwise a repeat run reports work it did not do, and ``changed`` asks for a reindex
    that nothing needs.
    """
    smb_server.add_directory("ImpromptuApp")
    steps: list[SyncProgress] = []

    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp/songs", suffixes={".mid"}, progress=steps.append
    )

    assert "ImpromptuApp" not in result.directories
    assert steps[0].action is SyncAction.SKIP
    assert steps[0].path == "ImpromptuApp"


async def test_sync_dry_run_changes_nothing(share, smb_server, catalogue):
    """Reporting the plan is worth having on its own, and must not act on it."""
    result = await share.async_sync_directory(catalogue, "ImpromptuApp", dry_run=True)

    assert len(result.uploaded) == 3
    assert result.directories == ("ImpromptuApp", "ImpromptuApp/Chopin")
    assert smb_server.paths == set()


async def test_sync_leaves_extra_files_alone_by_default(share, smb_server, catalogue):
    """Pruning deletes data, so it does not happen unless it is asked for."""
    smb_server.add_file("ImpromptuApp/keep-me.mid", b"mine")

    result = await share.async_sync_directory(catalogue, "ImpromptuApp")

    assert result.removed == ()
    assert "ImpromptuApp/keep-me.mid" in smb_server.paths


async def test_sync_prunes_when_asked(share, smb_server, catalogue):
    """Pruning removes what the local tree no longer has, contents before directories."""
    smb_server.add_file("ImpromptuApp/gone.mid", b"old")
    smb_server.add_file("ImpromptuApp/Liszt/gone.mid", b"old")

    result = await share.async_sync_directory(catalogue, "ImpromptuApp", prune=True)

    assert set(result.removed) == {
        "ImpromptuApp/Liszt/gone.mid",
        "ImpromptuApp/gone.mid",
        "ImpromptuApp/Liszt",
    }
    # The ordering that matters is that a directory is empty when its turn comes.
    assert result.removed.index("ImpromptuApp/Liszt/gone.mid") < result.removed.index(
        "ImpromptuApp/Liszt"
    )
    assert "ImpromptuApp/Chopin/Ballade.mid" in smb_server.paths


async def test_sync_prune_keeps_directories_it_still_needs(
    share, smb_server, catalogue
):
    """A directory already on the share and still in use must survive the prune."""
    await share.async_sync_directory(catalogue, "ImpromptuApp")

    result = await share.async_sync_directory(catalogue, "ImpromptuApp", prune=True)

    assert result.removed == ()
    assert "ImpromptuApp/Chopin" in smb_server.paths


async def test_sync_stops_at_the_first_failure_by_default(share, smb_server, catalogue):
    """Silence about a failed transfer would be the worst of both behaviours."""
    smb_server.fail_on[("store", "ImpromptuApp/Chopin/Ballade.mid")] = smb_failure(
        "denied", STATUS_ACCESS_DENIED
    )

    with pytest.raises(DisklavierShareAuthError):
        await share.async_sync_directory(catalogue, "ImpromptuApp")


async def test_sync_carries_on_past_a_file_that_kills_the_session(
    share, smb_server, catalogue
):
    """A transport failure has to reach `failed` too, not escape past continue_on_error.

    An 865 MB run died on exactly this: a NetBIOS framing error on one file propagated out
    of the library untranslated, so the collector never saw it and the transfers already
    done were thrown away with the traceback.
    """
    path = "ImpromptuApp/Chopin/Ballade.mid"
    smb_server.fail_on[("store", path)] = NMBError("Invalid protocol header")

    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp", continue_on_error=True
    )

    assert [failure.path for failure in result.failed] == [path]
    assert "ImpromptuApp/Chopin/Nocturne.mid" in result.uploaded


async def test_a_missing_local_file_is_reported_as_one(share, smb_server, tmp_path):
    """Not "lost the SMB session": the socket is fine, the file is not there.

    ``OSError`` is how a dead socket announces itself, so a local read error raised inside
    the worker was being diagnosed as a lost session -- and cost a healthy connection a
    pointless reconnect on the way out.
    """
    with pytest.raises(FileNotFoundError):
        await share.async_upload(tmp_path / "not-there.mid", "a.mid")

    assert smb_server.connects == 1, "the session should not have been dropped"


async def test_an_unwritable_download_destination_is_reported_as_one(
    share, smb_server, tmp_path
):
    """The same, in the other direction."""
    smb_server.add_file("a.mid", b"data")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            await share.async_download("a.mid", locked / "out.mid")
    finally:
        locked.chmod(0o700)


async def test_sync_progress_reaches_its_total_even_when_a_file_fails(
    share, smb_server, catalogue
):
    """A bar driven by this must not stick below 100% because one file was stepped over."""
    smb_server.fail_on[("store", "ImpromptuApp/Chopin/Ballade.mid")] = smb_failure(
        "denied", STATUS_ACCESS_DENIED
    )
    steps: list[SyncProgress] = []

    await share.async_sync_directory(
        catalogue, "ImpromptuApp", continue_on_error=True, progress=steps.append
    )

    assert steps[-1].index == steps[-1].total
    assert [step.action for step in steps].count(SyncAction.FAIL) == 1


async def test_delete_tree_removes_a_file_it_is_pointed_at(share, smb_server):
    """Walking a file asks the server to list it, which answers "no such path" -- untrue."""
    smb_server.add_file("dir/a.mid", b"x")

    assert await share.async_delete_tree("dir/a.mid") == ["dir/a.mid"]
    assert "dir/a.mid" not in smb_server.paths
    assert "dir" in smb_server.paths

    with pytest.raises(DisklavierShareNotFoundError):
        await share.async_delete_tree("dir/nope.mid")


async def test_sync_can_carry_on_past_a_failure(share, smb_server, catalogue):
    """One bad file should not undo a long run; it should be reported and stepped over."""
    smb_server.fail_on[("store", "ImpromptuApp/Chopin/Ballade.mid")] = smb_failure(
        "denied", STATUS_ACCESS_DENIED
    )

    result = await share.async_sync_directory(
        catalogue, "ImpromptuApp", continue_on_error=True
    )

    assert [failure.path for failure in result.failed] == [
        "ImpromptuApp/Chopin/Ballade.mid"
    ]
    assert "denied" in result.failed[0].error
    assert "ImpromptuApp/Chopin/Nocturne.mid" in result.uploaded


async def test_sync_into_the_share_root(share, smb_server, catalogue):
    """Mirroring onto the root needs no destination directory of its own."""
    result = await share.async_sync_directory(catalogue, suffixes={".mid"})

    assert result.directories == ("Chopin",)
    assert "Chopin/Ballade.mid" in smb_server.paths


async def test_sync_warns_when_a_backing_track_is_filtered_out(share, tmp_path, caplog):
    """A MIDI sent without its audio still plays -- as a bare piano part. Say so."""
    root = tmp_path / "transcribed"
    root.mkdir()
    (root / "Patient.mid").write_bytes(b"MThd")
    (root / "Patient.wav").write_bytes(b"RIFF" * 100)

    with caplog.at_level("WARNING"):
        result = await share.async_sync_directory(
            root, "ImpromptuApp", suffixes={".mid"}
        )

    assert result.uploaded == ("ImpromptuApp/Patient.mid",)
    assert "backing audio track" in caplog.text
    assert "Patient.mid" in caplog.text


async def test_sync_is_quiet_when_the_audio_comes_along(share, tmp_path, caplog):
    """PLAYABLE_SUFFIXES takes the audio too, which is the whole point of it."""
    root = tmp_path / "transcribed"
    root.mkdir()
    (root / "Patient.mid").write_bytes(b"MThd")
    (root / "Patient.wav").write_bytes(b"RIFF" * 100)

    with caplog.at_level("WARNING"):
        result = await share.async_sync_directory(
            root, "ImpromptuApp", suffixes=PLAYABLE_SUFFIXES
        )

    assert set(result.uploaded) == {
        "ImpromptuApp/Patient.mid",
        "ImpromptuApp/Patient.wav",
    }
    assert "backing audio" not in caplog.text


async def test_sync_does_not_warn_about_audio_with_no_midi(share, tmp_path, caplog):
    """A stray render with no MIDI beside it is not a song missing its band."""
    root = tmp_path / "renders"
    root.mkdir()
    (root / "vocal stem.wav").write_bytes(b"RIFF")
    (root / "Patient.mid").write_bytes(b"MThd")

    with caplog.at_level("WARNING"):
        await share.async_sync_directory(root, "ImpromptuApp", suffixes={".mid"})

    assert "backing audio" not in caplog.text


async def test_sync_warns_about_files_the_piano_will_never_index(
    share, tmp_path, caplog
):
    """Copying succeeds and the library stays empty, so the silence has to be broken."""
    root = tmp_path / "deep"
    (root / "maestro" / "Chopin").mkdir(parents=True)
    (root / "maestro" / "Chopin" / "Ballade.mid").write_bytes(b"MThd")

    with caplog.at_level("WARNING"):
        result = await share.async_sync_directory(root, "ImpromptuApp")

    assert result.uploaded == ("ImpromptuApp/maestro/Chopin/Ballade.mid",)
    assert "deeper than the 2 folder levels" in caplog.text


async def test_sync_is_quiet_at_the_indexable_depth(share, catalogue, caplog):
    """The warning has to stay rare, or it stops being read."""
    with caplog.at_level("WARNING"):
        await share.async_sync_directory(catalogue, "ImpromptuApp", suffixes={".mid"})

    assert "deeper than" not in caplog.text


async def test_sync_needs_a_directory(share, tmp_path):
    """Pointing it at a file is a mistake worth catching locally."""
    lonely = tmp_path / "song.mid"
    lonely.write_bytes(b"x")

    with pytest.raises(ValueError, match="not a directory"):
        await share.async_sync_directory(lonely, "ImpromptuApp")


def test_needs_upload_replaces_a_directory_in_the_way(tmp_path):
    """A directory sitting where a file belongs is a conflict, not a match."""
    from aiodisklavier.share import _LocalFile

    local = _LocalFile(source=tmp_path, relative="a.mid", size=0, mtime=0.0)
    blocking = ShareEntry(
        name="a.mid",
        path="a.mid",
        is_directory=True,
        size=0,
        modified=datetime.fromtimestamp(0, tz=UTC),
    )

    assert _needs_upload(local, blocking)


# ----------------------------------------------------------------------
# Packaging
# ----------------------------------------------------------------------


def test_share_types_are_exported_from_the_package_root():
    """A caller should not have to know which module a name lives in."""
    import aiodisklavier

    exported = {
        "DisklavierShare",
        "ShareEntry",
        "SyncAction",
        "SyncFailure",
        "SyncProgress",
        "SyncResult",
        "INDEXED_DEPTH_LIMIT",
    }
    assert exported <= set(aiodisklavier.__all__)
    assert all(hasattr(aiodisklavier, name) for name in aiodisklavier.__all__)
    assert aiodisklavier.DisklavierShare is DisklavierShare


def test_fake_server_matches_the_backend_protocol(smb_server):
    """The double is only worth anything if it satisfies the protocol the client uses."""
    from aiodisklavier.share import SMBBackend

    assert isinstance(smb_server.backend(), SMBBackend)
