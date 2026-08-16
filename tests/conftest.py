"""Shared fixtures for the aiodisklavier test suite.

Tests run against a real aiohttp server that imitates the piano, rather than a mocking
layer. That keeps the suite independent of aiohttp internals, and it means assertions about
the query string are checking what genuinely went over the wire -- which matters, because
the firmware's valueless flag arguments are easy to encode wrongly.

The SMB share gets the same treatment as far as it can: :class:`FakeSMBServer` is a small
in-memory filesystem that answers with the NT status codes the piano's Samba was observed
to return, so the error mapping is tested against real codes rather than invented ones. It
stands in for ``pysmb`` through the ``connection_factory`` seam, which keeps the suite from
needing a live SMB server without letting it drift into testing a mock.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import IO, Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from smb.base import NotConnectedError, SharedDevice, SharedFile
from smb.smb_structs import OperationFailure, SMBMessage

from aiodisklavier import Disklavier
from aiodisklavier.const import (
    PATH_API_BASE,
    PATH_CURRENT_INFO,
    PATH_STATIC_INFO,
    SHARE_PC_SHARING,
)
from aiodisklavier.share import DisklavierShare, SMBBackend

# NT status codes, as the piano's Samba 3.0.37 reports them. Verified on hardware.
STATUS_NAME_NOT_FOUND = 0xC0000034
STATUS_PATH_NOT_FOUND = 0xC000003A
STATUS_NAME_COLLISION = 0xC0000035
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_ACCESS_DENIED = 0xC0000022


STATIC_INFO_PAYLOAD = {
    "api_version": "1.0",
    "api_revision": "1",
    "disklavier_id": "DKV000000000000",
    "enspire_region": "World",
    "enspire_version": "5.24.00",
    "enspire_model": "PRO",
    "piano_type": "grand",
}

CURRENT_INFO_PAYLOAD = {
    "power_status": "on",
    "quiet_status": "acoustic",
    "playback_status": "pause",
    "playback_position": "516000",
    "volume_main": "100",
    "song_title": "Beethoven - Symphony No. 7, Movement 1.",
    "song_artist": "Ludwig van Beethoven",
    "song_folder": "Liszt's Beethoven's Symphony No. 7",
    "song_length": "851900",
}

MASTER_PAYLOAD = {
    "repeat": "off",
    "seq": {
        "status": "pause",
        "time": 516000,
        "tempo": 100,
        # The real device reports both, and restore depends on them.
        "song_pfix": "y",
        "song_id": "24",
    },
    "vol": {"main": 100, "headphone": 70},
    "piano": {
        "quiet": "acoustic",
        "met_status": "disable",
        "met_tempo": "120",
        "met_beat": "4/4",
        "key_motion": "on",
    },
    "sbc": {"headphone": "disconnected", "usb": "connected"},
}

# A miniature /ctrl/song.json: the sections the client reads, shaped as the firmware
# serves them -- rows keyed by prefix+id, every scalar a string.
SONG_DB_PAYLOAD = {
    "lang": "en",
    "update": "40157193",
    "song": {
        "d1": {
            "pfix": "d",
            "song_id": "1",
            "song_title": "Angel",
            "format": "SMF,MP3",
            "length": "350760",
            "album_id": "1",
            "genre": "Pop",
            "composer": "",
            "performer": "Sarah McLachlan",
        },
        "d250": {
            "pfix": "d",
            "song_id": "250",
            "song_title": "Invention 1",
            "format": "SMFSOLO",
            "length": "69479",
            "album_id": "9",
            "genre": "Classical",
            "composer": "J. S. Bach",
            "performer": "",
        },
        "y24": {
            "pfix": "y",
            "song_id": "24",
            "song_title": "Clair de lune",
            "format": "SMFXG",
            "length": "300000",
            "album_id": "2",
            "genre": "Classical",
            "composer": "Claude Debussy",
            "performer": "",
        },
        "f7": {
            "pfix": "f",
            "song_id": "7",
            "song_title": "Someone Like You",
            "format": "SMF",
            "length": "324208",
            "album_id": "22",
            "genre": "",
            "composer": "",
            "performer": "Adele",
        },
        # An unmapped prefix: parsed into the database, excluded from search results.
        "q9": {
            "pfix": "q",
            "song_id": "9",
            "song_title": "Clair de lune (mystery copy)",
            "format": "SMF",
            "length": "1000",
            "album_id": "1",
        },
        # A row with no identity: dropped during parsing.
        "broken": {"song_title": "No ids"},
    },
}


def dumps(payload: object) -> str:
    """Serialise a payload the way the firmware would."""
    return json.dumps(payload)


@dataclass
class Request:
    """One request the fake piano received."""

    path: str
    query_string: str
    query: dict[str, str]

    @property
    def command(self) -> str | None:
        """The open API command, for requests to the versioned ``/api/1.0/`` path."""
        prefix = f"{PATH_API_BASE}/"
        if not self.path.startswith(prefix):
            return None
        return self.path[len(prefix) :]


@dataclass
class FakePiano:
    """A stand-in for the piano's HTTP API."""

    requests: list[Request] = field(default_factory=list)

    #: Body returned by the next open API command. Defaults to an empty command reply.
    command_body: str = ""
    #: HTTP status returned by the next open API command.
    command_status: int = 200
    #: Per-command status overrides, so one command can fail while the rest work.
    command_status_for: dict[str, int] = field(default_factory=dict)
    #: Per-command body overrides, so one command can answer differently from the rest.
    command_body_for: dict[str, str] = field(default_factory=dict)
    #: Extra headers on open API command replies, e.g. a Location for a redirect.
    command_headers: dict[str, str] = field(default_factory=dict)
    #: Body returned by /api/current_info.
    current_body: str = field(default_factory=lambda: dumps(CURRENT_INFO_PAYLOAD))
    #: Raw bytes for /api/current_info, taking precedence over ``current_body``. Lets a
    #: test serve byte-exact failure shapes, like a read cut inside a multibyte character.
    current_raw: bytes | None = None
    #: When set, /api/current_info promises a body, sends part of it, and drops the
    #: connection. Distinct from ``current_raw`` truncation, which completes at the HTTP
    #: level: this one fails mid-transfer.
    drop_mid_body: bool = False
    #: Called after each current_info request, to let a test change what comes next.
    on_current_info: Callable[[], None] | None = None
    #: Seconds to stall every response, for exercising client-side timeouts.
    delay: float = 0.0
    #: Body returned by /ctrl/song.json. Defaults to a small but complete database.
    song_db_body: str = field(default_factory=lambda: dumps(SONG_DB_PAYLOAD))

    async def _record(self, request: web.Request) -> None:
        self.requests.append(
            Request(
                path=request.path,
                query_string=request.query_string,
                query=dict(request.query),
            )
        )
        if self.delay:
            await asyncio.sleep(self.delay)

    @property
    def last(self) -> Request:
        """The most recent request."""
        return self.requests[-1]

    def build_app(self) -> web.Application:
        """Build the aiohttp application."""

        async def static_info(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text=dumps(STATIC_INFO_PAYLOAD))

        async def current_info(request: web.Request) -> web.StreamResponse:
            await self._record(request)
            if self.drop_mid_body:
                response = web.StreamResponse()
                response.content_length = 1000
                await response.prepare(request)
                await response.write(b'{"power_status": "on"')
                assert request.transport is not None
                request.transport.close()
                return response
            body = self.current_body
            raw = self.current_raw
            if self.on_current_info is not None:
                self.on_current_info()
            if raw is not None:
                return web.Response(body=raw)
            return web.Response(text=body)

        async def master_json(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text=dumps(MASTER_PAYLOAD))

        async def song_db(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text=self.song_db_body)

        async def command(request: web.Request) -> web.Response:
            await self._record(request)
            name = request.path.rpartition("/")[2]
            return web.Response(
                text=self.command_body_for.get(name, self.command_body),
                status=self.command_status_for.get(name, self.command_status),
                headers=self.command_headers,
            )

        async def ctrl(request: web.Request) -> web.Response:
            await self._record(request)
            return web.Response(text="")

        app = web.Application()
        # Order matters: the specific state paths must be registered before the catch-all
        # command route, which would otherwise swallow them.
        app.router.add_get(PATH_STATIC_INFO, static_info)
        app.router.add_get(PATH_CURRENT_INFO, current_info)
        app.router.add_get(f"{PATH_API_BASE}/{{command}}", command)
        app.router.add_get("/ctrl/master.json", master_json)
        app.router.add_get("/ctrl/song.json", song_db)
        app.router.add_get("/ctrl/setSeq.php", ctrl)
        app.router.add_get("/ctrl/setSong.php", ctrl)
        app.router.add_get("/ctrl/putNoteOn.php", ctrl)
        app.router.add_get("/ctrl/setRefreshDB.php", ctrl)
        return app


@pytest.fixture
async def fake_piano() -> FakePiano:
    """Provide the fake piano's state and request log."""
    return FakePiano()


@pytest.fixture
async def server(fake_piano: FakePiano) -> AsyncIterator[TestServer]:
    """Run the fake piano."""
    test_server = TestServer(fake_piano.build_app())
    await test_server.start_server()
    try:
        yield test_server
    finally:
        await test_server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide an aiohttp session."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def piano(server: TestServer, session: aiohttp.ClientSession) -> Disklavier:
    """Provide a client pointed at the fake piano."""
    assert server.port is not None
    return Disklavier(server.host, session, port=server.port)


@pytest.fixture
async def offline_piano(session: aiohttp.ClientSession) -> Disklavier:
    """Provide a client pointed at a port with nothing listening."""
    # Port 1 is reserved and never has a listener, so connections fail immediately.
    return Disklavier("127.0.0.1", session, port=1, timeout=2)


# ----------------------------------------------------------------------
# SMB
# ----------------------------------------------------------------------


def smb_failure(message: str, status: int) -> OperationFailure:
    """Build the failure ``pysmb`` raises, carrying an NT status the way it does."""
    smb_message = SMBMessage()
    smb_message.status.internal_value = status
    return OperationFailure(message, [smb_message])


def shared_file(name: str, *, size: int, directory: bool, mtime: float) -> SharedFile:
    """Build the listing row ``pysmb`` returns."""
    return SharedFile(
        mtime,  # create_time
        mtime,  # last_access_time
        mtime,  # last_write_time
        mtime,  # last_attr_change_time
        size,
        size,  # alloc_size
        0x10 if directory else 0x80,  # file_attributes
        name,  # short_name
        name,
    )


@dataclass
class FakeNode:
    """One file or directory in the fake share."""

    mtime: float
    #: ``None`` marks a directory. Files hold their contents.
    data: bytes | None = None

    @property
    def is_directory(self) -> bool:
        """Whether this node is a directory."""
        return self.data is None


@dataclass
class FakeSMBServer:
    """An in-memory stand-in for the piano's SMB service.

    Paths are share-relative and POSIX-style throughout, matching what the client sends
    once it has normalised them. The share root always exists and is never listed.
    """

    nodes: dict[str, FakeNode] = field(default_factory=dict)
    shares: list[str] = field(default_factory=lambda: [SHARE_PC_SHARING, "IPC$"])
    #: Every operation performed, as ``(operation, path)``.
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: Exceptions to raise instead of performing the next operations, in order. Lets a
    #: test drop the session underneath the client and watch it reconnect.
    fail_next: list[BaseException] = field(default_factory=list)
    #: Exceptions to raise for particular ``(operation, path)`` pairs, every time they
    #: come up. For failing one file of many, where ``fail_next`` cannot aim.
    fail_on: dict[tuple[str, str], BaseException] = field(default_factory=dict)
    #: What ``connect`` reports, and how many times it has been called.
    connect_result: bool = True
    connect_error: BaseException | None = None
    connects: int = 0
    closes: int = 0
    #: Every timeout the client passed down, so a test can prove it reaches the wire.
    timeouts: list[int] = field(default_factory=list)
    #: Timestamp stamped onto anything written. Wall clock by default, because that is
    #: what the piano does -- its clock was found to agree with the host's, and sync
    #: compares a local file's mtime against the time its copy was written.
    now: float = field(default_factory=time.time)

    # -- helpers used by tests ----------------------------------------

    def add_file(self, path: str, data: bytes, *, mtime: float | None = None) -> None:
        """Seed a file, creating the directories above it."""
        for parent in _fake_parents(path):
            self.nodes.setdefault(parent, FakeNode(mtime=self.now))
        self.nodes[path] = FakeNode(
            mtime=self.now if mtime is None else mtime, data=data
        )

    def add_directory(self, path: str) -> None:
        """Seed a directory, creating the directories above it."""
        for candidate in [*_fake_parents(path), path]:
            self.nodes.setdefault(candidate, FakeNode(mtime=self.now))

    @property
    def paths(self) -> set[str]:
        """Every path currently present."""
        return set(self.nodes)

    def backend(self) -> SMBBackend:
        """Build a client-facing connection onto this server."""
        return FakeSMBConnection(self)

    # -- the operations themselves ------------------------------------

    def _record(self, operation: str, path: str) -> None:
        self.calls.append((operation, path))
        planned = self.fail_on.get((operation, path))
        if planned is not None:
            raise planned
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _require(self, path: str, operation: str) -> FakeNode:
        node = self.nodes.get(path)
        if node is None:
            raise smb_failure(f"Failed to {operation} {path}", STATUS_NAME_NOT_FOUND)
        return node

    def _require_parent(self, path: str, operation: str) -> None:
        parent = path.rpartition("/")[0]
        if parent and parent not in self.nodes:
            raise smb_failure(f"Failed to {operation} {path}", STATUS_PATH_NOT_FOUND)

    def list_path(self, path: str) -> list[SharedFile]:
        """List a directory, including the dot entries a real server reports."""
        self._record("list", path)
        if path:
            node = self._require(path, "list")
            if not node.is_directory:
                raise smb_failure(f"Not a directory: {path}", STATUS_PATH_NOT_FOUND)
        rows = [
            shared_file(name, size=0, directory=True, mtime=self.now)
            for name in (".", "..")
        ]
        prefix = f"{path}/" if path else ""
        for candidate, node in sorted(self.nodes.items()):
            name = candidate[len(prefix) :]
            if not candidate.startswith(prefix) or not name or "/" in name:
                continue
            rows.append(
                shared_file(
                    name,
                    size=0 if node.data is None else len(node.data),
                    directory=node.is_directory,
                    mtime=node.mtime,
                )
            )
        return rows

    def get_attributes(self, path: str) -> SharedFile:
        """Stat one path."""
        self._record("stat", path)
        if not path:
            return shared_file("", size=0, directory=True, mtime=self.now)
        node = self._require(path, "get attributes for")
        return shared_file(
            path.rpartition("/")[2],
            size=0 if node.data is None else len(node.data),
            directory=node.is_directory,
            mtime=node.mtime,
        )

    def store_file(self, path: str, handle: IO[bytes]) -> int:
        """Write a file, replacing whatever was there."""
        self._record("store", path)
        self._require_parent(path, "store")
        data = handle.read()
        self.nodes[path] = FakeNode(mtime=self.now, data=data)
        return len(data)

    def retrieve_file(self, path: str, handle: IO[bytes]) -> tuple[Any, int]:
        """Read a file into an open stream."""
        self._record("retrieve", path)
        node = self._require(path, "retrieve")
        assert node.data is not None
        handle.write(node.data)
        return None, len(node.data)

    def create_directory(self, path: str) -> None:
        """Create one directory."""
        self._record("mkdir", path)
        if path in self.nodes:
            raise smb_failure(f"Failed to create {path}", STATUS_NAME_COLLISION)
        self._require_parent(path, "create")
        self.nodes[path] = FakeNode(mtime=self.now)

    def delete_directory(self, path: str) -> None:
        """Remove an empty directory."""
        self._record("rmdir", path)
        self._require(path, "remove")
        if any(other.startswith(f"{path}/") for other in self.nodes):
            raise smb_failure(
                f"Directory not empty: {path}", STATUS_DIRECTORY_NOT_EMPTY
            )
        del self.nodes[path]

    def delete_files(self, path: str) -> None:
        """Delete a file."""
        self._record("unlink", path)
        self._require(path, "delete")
        del self.nodes[path]

    def rename(self, old: str, new: str) -> None:
        """Rename within the share."""
        self._record("rename", old)
        node = self._require(old, "rename")
        self._require_parent(new, "rename")
        del self.nodes[old]
        self.nodes[new] = node


def _fake_parents(path: str) -> list[str]:
    """List a path's ancestors, shallowest first."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: index + 1]) for index in range(len(parts))]


class FakeSMBConnection:
    """The :class:`~aiodisklavier.share.SMBBackend` face of a :class:`FakeSMBServer`.

    Takes the absolute, backslash-free paths the client sends and hands the server the
    share-relative ones it works in, which is also where a client that forgot to normalise
    a path would be caught.
    """

    def __init__(self, server: FakeSMBServer) -> None:
        """Bind to a server."""
        self._server = server
        self._closed = False

    @staticmethod
    def _relative(path: str) -> str:
        assert path.startswith("/"), f"path should reach the backend absolute: {path!r}"
        return path[1:]

    def _live(self) -> FakeSMBServer:
        if self._closed:
            raise NotConnectedError("connection is closed")
        return self._server

    def connect(
        self, ip: str, port: int, sock_family: int | None, timeout: int
    ) -> bool:
        """Open the session."""
        # pysmb reads the argument in this position as a socket family, so anything but
        # None here means the client has miscounted its positional arguments.
        assert sock_family is None, f"sock_family should be None, got {sock_family!r}"
        self._server.connects += 1
        self._server.timeouts.append(timeout)
        if self._server.connect_error is not None:
            raise self._server.connect_error
        return self._server.connect_result

    def close(self) -> None:
        """Close the session."""
        self._closed = True
        self._server.closes += 1

    def listShares(self, *, timeout: int) -> list[Any]:
        """List the server's shares."""
        self._server.timeouts.append(timeout)
        return [SharedDevice(0, name, "") for name in self._live().shares]

    def listPath(self, service_name: str, path: str, *, timeout: int) -> list[Any]:
        """List a directory."""
        self._server.timeouts.append(timeout)
        return self._live().list_path(self._relative(path))

    def getAttributes(self, service_name: str, path: str, *, timeout: int) -> Any:
        """Stat one path."""
        self._server.timeouts.append(timeout)
        return self._live().get_attributes(self._relative(path))

    def storeFile(
        self, service_name: str, path: str, file_obj: IO[bytes], *, timeout: int
    ) -> int:
        """Write a file."""
        self._server.timeouts.append(timeout)
        return self._live().store_file(self._relative(path), file_obj)

    def retrieveFile(
        self, service_name: str, path: str, file_obj: IO[bytes], *, timeout: int
    ) -> tuple[Any, int]:
        """Read a file."""
        self._server.timeouts.append(timeout)
        return self._live().retrieve_file(self._relative(path), file_obj)

    def createDirectory(self, service_name: str, path: str, *, timeout: int) -> None:
        """Create one directory."""
        self._server.timeouts.append(timeout)
        self._live().create_directory(self._relative(path))

    def deleteDirectory(self, service_name: str, path: str, *, timeout: int) -> None:
        """Remove an empty directory."""
        self._server.timeouts.append(timeout)
        self._live().delete_directory(self._relative(path))

    def deleteFiles(self, service_name: str, path: str, *, timeout: int) -> None:
        """Delete a file."""
        self._server.timeouts.append(timeout)
        self._live().delete_files(self._relative(path))

    def rename(
        self, service_name: str, old_path: str, new_path: str, *, timeout: int
    ) -> None:
        """Rename within the share."""
        self._server.timeouts.append(timeout)
        self._live().rename(self._relative(old_path), self._relative(new_path))


@pytest.fixture
def smb_server() -> FakeSMBServer:
    """Provide an empty in-memory SMB share."""
    return FakeSMBServer()


@pytest.fixture
async def share(smb_server: FakeSMBServer) -> AsyncIterator[DisklavierShare]:
    """Provide a share client wired to the in-memory server."""
    client = DisklavierShare("piano.local", connection_factory=smb_server.backend)
    try:
        yield client
    finally:
        await client.async_close()
