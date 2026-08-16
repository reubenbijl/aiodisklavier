"""Async access to the Disklavier's SMB shares.

The piano exports its *PC Sharing Folder* over SMB, and that share is the only way to put
new MIDI onto the instrument: copy a file in, call
:meth:`~aiodisklavier.Disklavier.async_refresh_library` to reindex, and it becomes
playable. This module wraps that share in the same async, typed idiom as the HTTP client.

Example::

    async with DisklavierShare("192.168.1.50") as share:
        for entry in await share.async_list():
            print(entry.path, entry.size)

        await share.async_upload("doorbell.mid", "ImpromptuApp/doorbell.mid")

    await piano.async_refresh_library()

**On the protocol.** The firmware runs Samba 3.0.37, which predates SMB2 and speaks NT1
only -- an SMB2 negotiate gets the socket closed without a reply. This module therefore
uses ``pysmb``, which still speaks NT1; ``smbprotocol`` cannot talk to the piano at all.
``pysmb`` negotiates SMB2 where a server offers it, so nothing here depends on the piano
staying on NT1 forever.

**On threads.** ``pysmb`` is synchronous and its connection object owns one socket with a
single request stream, so every call runs in a worker thread behind a lock. Operations on
one :class:`DisklavierShare` are therefore serialised, however many tasks await them; use
separate instances if you want genuine parallelism.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import IO, Any, Final, Protocol, TypeVar, runtime_checkable

from nmb.nmb_structs import NMBError
from smb.base import NotConnectedError, NotReadyError, SMBTimeout
from smb.smb_structs import OperationFailure, ProtocolError
from smb.SMBConnection import SMBConnection

from .const import (
    AUDIO_SUFFIXES,
    DEFAULT_EXCLUDES,
    INDEXED_DEPTH_LIMIT,
    MTIME_TOLERANCE,
    SHARE_PC_SHARING,
    SMB_CLIENT_NAME,
    SMB_GUEST_USER,
    SMB_PORT,
    SMB_SERVER_NAME,
    SMB_TIMEOUT,
)
from .exceptions import (
    DisklavierConnectionError,
    DisklavierShareAuthError,
    DisklavierShareError,
    DisklavierShareExistsError,
    DisklavierShareNotFoundError,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Entries every SMB server reports in a directory listing, and no caller wants.
_DOT_ENTRIES: Final = (".", "..")


def _is_plain_name(name: str) -> bool:
    """Whether a listing row's name is a filename and only a filename.

    SMB cannot express a separator inside a name, so a row carrying one -- or spelling a
    relative path -- did not come from a well-behaved server. Such a name is dropped rather
    than joined onto a parent, because the joined result is what a caller hands to
    ``Path(local_dir) / entry.path``, and ``dir/../../../etc/passwd`` escapes when they do.
    This library's own calls would be stopped by :func:`_clean` on the way back, but the
    paths it *hands out* have to be safe too.
    """
    return bool(name) and name not in _DOT_ENTRIES and not set(name) & {"/", "\\"}


# NT status codes, mapped to exceptions so callers can branch on cause rather than on the
# text of a message. Confirmed against the piano's Samba: a missing leaf answers
# OBJECT_NAME_NOT_FOUND, a missing parent answers OBJECT_PATH_NOT_FOUND.
_STATUS_NOT_FOUND: Final = frozenset(
    {
        0xC0000034,  # OBJECT_NAME_NOT_FOUND
        0xC0000039,  # OBJECT_PATH_INVALID
        0xC000003A,  # OBJECT_PATH_NOT_FOUND
    }
)
_STATUS_EXISTS: Final = frozenset(
    {
        0xC0000035,  # OBJECT_NAME_COLLISION
        0xC0000101,  # DIRECTORY_NOT_EMPTY -- reported when replacing a populated name
    }
)
_STATUS_DENIED: Final = frozenset(
    {
        0xC0000022,  # ACCESS_DENIED
        0xC000006D,  # LOGON_FAILURE
        0xC000006E,  # ACCOUNT_RESTRICTION
        0xC0000072,  # ACCOUNT_DISABLED
    }
)

#: ``pysmb`` failures that mean the session is gone rather than the request being wrong.
#: These are worth one silent reconnect; anything else is a real answer from the server.
#:
#: ``OSError`` covers the socket layer, including ``ConnectionResetError`` and the
#: ``TimeoutError`` a stalled read raises. ``NMBError`` is the NetBIOS framing layer beneath
#: SMB, and it is the one that catches people out: it derives straight from ``Exception``,
#: and ``nmb`` ships its own ``NotConnectedError`` that is a different class from
#: ``smb.base``'s, so neither is caught by anything aimed at the SMB layer. A framing error
#: -- "Invalid protocol header for Direct TCP session message" -- means the byte stream has
#: lost sync, which no amount of reading will recover; only a new socket will. Observed
#: partway through an 865 MB transfer to the piano.
_CONNECTION_LOST: Final = (
    NotConnectedError,
    SMBTimeout,
    ProtocolError,
    NMBError,
    OSError,
)


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------


def _clean(path: str | PurePosixPath) -> str:
    """Normalise a caller-supplied path to a share-relative POSIX path.

    Accepts either separator and returns one without a leading slash, so the share root is
    the empty string. ``.`` segments are dropped.

    :raises ValueError: the path tries to climb out of the share with ``..``, or carries a
        control character. Neither can be a legitimate request, and both are the shapes a
        traversal uses, so they are refused here rather than sent to the server.
    """
    text = str(path)
    if any(character < " " or character == "\x7f" for character in text):
        raise ValueError(f"path contains a control character: {text!r}")

    parts: list[str] = []
    for part in text.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"path must stay inside the share: {text!r}")
        parts.append(part)
    return "/".join(parts)


def _wire(path: str) -> str:
    """Render a cleaned path the way the SMB layer wants it -- absolute, from the root."""
    return "/" + path


def _join(base: str, *parts: str) -> str:
    """Join already-cleaned path fragments."""
    return "/".join(part for part in (base, *parts) if part)


def _parents(path: str) -> list[str]:
    """List a path's ancestors, shallowest first, excluding the share root."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: index + 1]) for index in range(len(parts))]


def _matches(name: str, patterns: Collection[str]) -> bool:
    """Test a single path component against glob patterns, ignoring case.

    Case folding is explicit rather than left to :func:`fnmatch.fnmatch`, whose own
    behaviour follows the host platform -- which would make the same tree sync differently
    on macOS and on Linux.
    """
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def _timestamp(value: float) -> datetime:
    """Convert an SMB timestamp to an aware UTC datetime.

    A server that reports a nonsensical time degrades to the epoch rather than taking the
    whole listing down with it.
    """
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(0, tz=UTC)


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShareEntry:
    """One file or directory on the share."""

    name: str
    #: Path relative to the share root, POSIX-style. Empty for the root itself.
    path: str
    is_directory: bool
    size: int
    modified: datetime

    @classmethod
    def _from_shared_file(cls, shared_file: Any, parent: str) -> ShareEntry:
        """Build from a ``pysmb`` listing row."""
        name = str(shared_file.filename)
        return cls(
            name=name,
            path=_join(parent, name),
            is_directory=bool(shared_file.isDirectory),
            size=int(shared_file.file_size),
            modified=_timestamp(float(shared_file.last_write_time)),
        )


class _LocalFileError(Exception):
    """A local filesystem error raised from inside an SMB operation.

    Wrapped because ``OSError`` is the net :data:`_CONNECTION_LOST` uses to spot a dead
    socket, and a source file that is missing or unreadable is neither the server's doing
    nor curable by reconnecting. Unwrapped again at the boundary, so the caller sees the
    ``FileNotFoundError`` or ``PermissionError`` that actually happened.
    """

    def __init__(self, error: OSError) -> None:
        """Wrap a local error."""
        super().__init__(str(error))
        self.error = error


class SyncAction(StrEnum):
    """What :meth:`DisklavierShare.async_sync_directory` did with one path."""

    UPLOAD = "upload"
    SKIP = "skip"
    REMOVE = "remove"
    CREATE_DIRECTORY = "create_directory"
    #: Reported only when ``continue_on_error`` is set, so a progress bar still reaches its
    #: total when a file is stepped over rather than transferred.
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """One step of a sync, handed to the ``progress`` callback as it happens.

    The callback runs on the event loop, so it must not block.
    """

    action: SyncAction
    #: Path relative to the share root.
    path: str
    size: int
    #: Position within the run, 1-based, and the total number of steps planned.
    index: int
    total: int


@dataclass(frozen=True, slots=True)
class SyncFailure:
    """One path a sync could not transfer, collected when ``continue_on_error`` is set."""

    path: str
    error: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a sync did, as share-relative paths."""

    uploaded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    failed: tuple[SyncFailure, ...] = ()
    bytes_uploaded: int = 0

    @property
    def changed(self) -> bool:
        """Whether anything on the share actually moved.

        A reindex is only worth issuing when this is true.
        """
        return bool(self.uploaded or self.removed or self.directories)


@dataclass(frozen=True, slots=True)
class _LocalFile:
    """A file found in the local tree, with what a sync needs to decide about it."""

    source: Path
    #: Path relative to the local root, POSIX-style.
    relative: str
    size: int
    mtime: float


@dataclass
class _SyncPlan:
    """The mutations a sync intends, worked out before any of them are applied."""

    directories: list[str] = field(default_factory=list)
    uploads: list[tuple[_LocalFile, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Whole entries rather than paths, so applying the plan knows which are directories
    #: without asking the server again.
    removals: list[ShareEntry] = field(default_factory=list)

    @property
    def steps(self) -> int:
        """How many progress callbacks the plan will emit."""
        return (
            len(self.directories)
            + len(self.uploads)
            + len(self.skipped)
            + len(self.removals)
        )


# ----------------------------------------------------------------------
# Backend
# ----------------------------------------------------------------------


@runtime_checkable
class SMBBackend(Protocol):
    """The slice of ``pysmb``'s ``SMBConnection`` this module uses.

    Declared as a protocol so the transport can be substituted -- by a test double, or by
    a different SMB implementation should the firmware ever grow SMB2 and make one
    preferable. Method names are ``pysmb``'s.

    Every method takes a ``timeout``, and this client passes its own to all of them. Leaving
    them off would let ``pysmb``'s 30-second default stand, quietly making the constructor's
    ``timeout`` argument apply to opening the session and nothing else. Note that ``pysmb``
    treats it as an inactivity timeout per exchange rather than a budget for the whole
    operation, so a multi-megabyte transfer may legitimately run far longer than it.

    ``timeout`` is keyword-only here, deliberately. In ``pysmb`` it sits at a different
    position in almost every signature -- third in ``getAttributes``, fourth in
    ``storeFile``, *fifth* in ``listPath`` -- with optional arguments in front of it. Passing
    it positionally lands it in ``listPath``'s ``search`` mask, where it silently returns the
    wrong set of files, or in ``deleteFiles``'s ``delete_matching_folders``, where any
    non-zero timeout is truthy and deletes directories the caller did not name. Both were
    written and caught here; the keyword marker is what stops them being written again.
    """

    def connect(
        self, ip: str, port: int, sock_family: int | None, timeout: int
    ) -> bool:
        """Open the session. Returns whether authentication succeeded.

        ``sock_family`` is spelled out rather than left to default because it sits
        *between* the port and the timeout in ``pysmb``'s signature, and passing three
        positional arguments quietly lands the timeout in it. ``pysmb`` then builds a raw
        ``socket.socket(<timeout>)`` instead of the ``create_connection`` it should, which
        on macOS is an ``AF_INET6`` socket for a timeout of 30 -- close enough to working
        that the mistake survives testing, and a plain failure on any other platform where
        30 is not that constant. ``None`` is the documented value, and the one that lets
        the family be inferred from the address.
        """
        ...

    def close(self) -> None:
        """Close the session."""
        ...

    def listShares(self, *, timeout: int) -> list[Any]:
        """List the server's shares."""
        ...

    def listPath(self, service_name: str, path: str, *, timeout: int) -> list[Any]:
        """List a directory."""
        ...

    def getAttributes(self, service_name: str, path: str, *, timeout: int) -> Any:
        """Stat one path."""
        ...

    def storeFile(
        self, service_name: str, path: str, file_obj: IO[bytes], *, timeout: int
    ) -> int:
        """Write a file, replacing whatever was there."""
        ...

    def retrieveFile(
        self, service_name: str, path: str, file_obj: IO[bytes], *, timeout: int
    ) -> tuple[Any, int]:
        """Read a file into an open binary stream."""
        ...

    def createDirectory(self, service_name: str, path: str, *, timeout: int) -> None:
        """Create one directory."""
        ...

    def deleteDirectory(self, service_name: str, path: str, *, timeout: int) -> None:
        """Remove an empty directory."""
        ...

    def deleteFiles(self, service_name: str, path: str, *, timeout: int) -> None:
        """Delete a file."""
        ...

    def rename(
        self, service_name: str, old_path: str, new_path: str, *, timeout: int
    ) -> None:
        """Rename within the share."""
        ...


def _status_of(err: OperationFailure) -> int | None:
    """Pull the NT status out of a ``pysmb`` failure, if it carried one."""
    for message in err.smb_messages:
        if message.status.hasError:
            status: int = message.status.internal_value
            return status
    return None


def _translate(err: OperationFailure, what: str, path: str) -> DisklavierShareError:
    """Map a ``pysmb`` operation failure onto this library's exceptions.

    Only ``err.message`` is carried over. ``str(err)`` renders every SMB message in the
    exchange as a hex dump, which is pages of noise in a log and useless in a traceback.
    """
    status = _status_of(err)
    detail = err.message.strip() or what
    if status in _STATUS_NOT_FOUND:
        return DisklavierShareNotFoundError(
            f"No such path on the share: {path!r}", path=path, status=status
        )
    if status in _STATUS_EXISTS:
        return DisklavierShareExistsError(
            f"Path already exists on the share: {path!r}", path=path, status=status
        )
    if status in _STATUS_DENIED:
        return DisklavierShareAuthError(
            f"Share refused {what} of {path!r}: {detail}", path=path, status=status
        )
    return DisklavierShareError(
        f"Could not {what} {path!r}: {detail}", path=path, status=status
    )


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------


class DisklavierShare:
    """Client for one SMB share on a Disklavier.

    The connection is opened lazily on first use and reused afterwards, so a long-lived
    instance costs nothing while idle. Use it as an async context manager, or call
    :meth:`async_close` when finished.

    :param host: Hostname or IP address of the piano -- the same one the HTTP client uses.
    :param share: Which share to open. The default is the writable one.
    :param username: Ignored by stock firmware, which serves the share to guests.
    :param password: Only needed for a piano that has been given one.
    :param port: 445 for direct TCP, or 139 with ``direct_tcp=False``.
    :param timeout: Per-operation timeout in seconds.
    :param client_name: NetBIOS name to claim; only meaningful over port 139.
    :param server_name: NetBIOS name of the piano; only meaningful over port 139.
    :param use_ntlm_v2: Leave on. NTLMv1 also works against the piano, and is worse.
    :param direct_tcp: Whether to speak SMB straight over TCP rather than over NetBIOS.
    :param connection_factory: Build the underlying connection. The seam exists so a test
        -- or a different SMB implementation -- can stand in for ``pysmb``.
    """

    def __init__(
        self,
        host: str,
        *,
        share: str = SHARE_PC_SHARING,
        username: str = SMB_GUEST_USER,
        password: str = "",
        port: int = SMB_PORT,
        timeout: float = SMB_TIMEOUT,
        client_name: str = SMB_CLIENT_NAME,
        server_name: str = SMB_SERVER_NAME,
        use_ntlm_v2: bool = True,
        direct_tcp: bool = True,
        connection_factory: Callable[[], SMBBackend] | None = None,
    ) -> None:
        """Initialise the client. No connection is made until the first operation."""
        self._host = host
        self._share = share
        self._username = username
        self._password = password
        self._port = port
        self._timeout = timeout
        self._client_name = client_name
        self._server_name = server_name
        self._use_ntlm_v2 = use_ntlm_v2
        self._direct_tcp = direct_tcp
        self._factory = connection_factory or self._default_factory
        self._connection: SMBBackend | None = None
        # pysmb multiplexes nothing: one socket, one request in flight. The lock is what
        # makes concurrent awaits on this object safe rather than a corrupted stream.
        self._lock = asyncio.Lock()

    def _default_factory(self) -> SMBBackend:
        """Build a ``pysmb`` connection from the configured settings."""
        connection: SMBBackend = SMBConnection(
            self._username,
            self._password,
            self._client_name,
            self._server_name,
            use_ntlm_v2=self._use_ntlm_v2,
            is_direct_tcp=self._direct_tcp,
        )
        return connection

    @property
    def host(self) -> str:
        """The piano's host."""
        return self._host

    @property
    def share(self) -> str:
        """The share this client is pointed at."""
        return self._share

    @property
    def connected(self) -> bool:
        """Whether a session is currently open."""
        return self._connection is not None

    @property
    def _deadline(self) -> int:
        """The configured timeout, in the whole seconds ``pysmb`` wants."""
        return int(self._timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DisklavierShare:
        """Open the session."""
        await self.async_connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session."""
        await self.async_close()

    async def async_connect(self) -> None:
        """Open the session now, rather than on first use.

        Worth calling from a config flow, where the point is to find out whether the
        credentials and the share name are right before anything else is set up.
        """
        async with self._lock:
            await self._connect_locked()

    async def async_close(self) -> None:
        """Close the session. Safe to call when nothing is open."""
        async with self._lock:
            await self._close_locked()

    async def _connect_locked(self) -> SMBBackend:
        """Return the open connection, opening one if needed. Caller holds the lock."""
        if self._connection is not None:
            return self._connection

        connection = self._factory()
        try:
            # pysmb wants a whole number of seconds here.
            # sock_family=None: see SMBBackend.connect. It is the difference between
            # socket.create_connection -- which resolves the host and picks the family --
            # and a raw socket of whatever family the timeout happens to name.
            authenticated = await asyncio.to_thread(
                connection.connect, self._host, self._port, None, self._deadline
            )
        except (NotReadyError, OperationFailure) as err:
            await self._discard(connection)
            raise DisklavierShareAuthError(
                f"Disklavier at {self._host} refused the SMB session: {err}"
            ) from err
        except _CONNECTION_LOST as err:
            await self._discard(connection)
            raise DisklavierConnectionError(
                f"Could not reach the Disklavier share at {self._host}:{self._port}: {err}"
            ) from err

        if not authenticated:
            await self._discard(connection)
            raise DisklavierShareAuthError(
                f"Disklavier at {self._host} rejected the SMB credentials "
                f"for user {self._username!r}"
            )

        self._connection = connection
        return connection

    async def _close_locked(self) -> None:
        """Close and forget the connection. Caller holds the lock."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await self._discard(connection)

    @staticmethod
    async def _discard(connection: SMBBackend) -> None:
        """Close a connection we are giving up on, ignoring how that goes.

        Closing is cleanup; a failure here would replace whatever real error led us to
        drop the connection in the first place.
        """
        with suppress(Exception):
            await asyncio.to_thread(connection.close)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _call(
        self, what: str, path: str, operation: Callable[[SMBBackend], _T]
    ) -> _T:
        """Run one blocking SMB operation in a worker thread, with a single retry.

        The piano drops idle sessions, so the first sign of trouble on a long-lived client
        is routinely a dead socket rather than a real fault. One reconnect absorbs that
        without every caller having to. A second failure is the caller's to hear about.
        """
        async with self._lock:
            try:
                return await self._attempt(what, path, operation)
            except DisklavierConnectionError as err:
                _LOGGER.debug(
                    "SMB session to %s lost during %s of %r (%s), reconnecting",
                    self._host,
                    what,
                    path,
                    err,
                )
                await self._close_locked()
            return await self._attempt(what, path, operation)

    async def _call_local(
        self, what: str, path: str, operation: Callable[[SMBBackend], _T]
    ) -> _T:
        """Run an operation that also touches the local filesystem.

        Unwraps :class:`_LocalFileError` so a local problem arrives as the ``OSError`` it
        is, rather than as the lost-session diagnosis ``_attempt`` would otherwise give it.
        """
        try:
            return await self._call(what, path, operation)
        except _LocalFileError as err:
            raise err.error from None

    async def _attempt(
        self, what: str, path: str, operation: Callable[[SMBBackend], _T]
    ) -> _T:
        """Run the operation once, mapping ``pysmb``'s failures onto ours."""
        connection = await self._connect_locked()
        try:
            return await asyncio.to_thread(operation, connection)
        except asyncio.CancelledError:
            # A cancelled await does not stop the worker: it is still mid-request on this
            # socket, and the lock is about to be released. Forget the connection so the
            # next operation opens a fresh session rather than interleaving with the
            # orphan -- two writers on one SMB stream desynchronise it, which surfaces
            # later as a framing error on some unrelated call.
            #
            # Forgotten rather than closed, deliberately: closing the socket underneath a
            # thread still writing to it trades one race for another. The orphan finishes
            # against a connection nobody else can reach, and goes when it does.
            self._connection = None
            raise
        except OperationFailure as err:
            # The server answered and refused. Retrying would only ask again.
            raise _translate(err, what, path) from err
        except NotReadyError as err:
            raise DisklavierShareAuthError(
                f"SMB session to {self._host} is not authenticated: {err}"
            ) from err
        except _CONNECTION_LOST as err:
            await self._close_locked()
            raise DisklavierConnectionError(
                f"Lost the SMB session to {self._host} during {what} of {path!r}: {err}"
            ) from err

    # ------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------

    async def async_list_shares(self) -> list[str]:
        """List the shares the piano exports.

        Stock firmware offers ``PC Sharing Folder`` and the read-only
        ``ENSPIRE Controller``, alongside the usual ``IPC$`` pipe.
        """
        shares = await self._call(
            "list shares", "", lambda c: c.listShares(timeout=self._deadline)
        )
        return [str(entry.name) for entry in shares]

    async def async_list(self, path: str | PurePosixPath = "") -> list[ShareEntry]:
        """List one directory, sorted with directories first then by name.

        The ``.`` and ``..`` rows are dropped, along with any name that is not a plain
        filename -- see :func:`_is_plain_name` for why a server should never send one and
        what it would cost to pass it on.

        :param path: Share-relative path. The default lists the share root.
        """
        clean = _clean(path)
        rows = await self._call(
            "list",
            clean,
            lambda c: c.listPath(self._share, _wire(clean), timeout=self._deadline),
        )
        entries = []
        for row in rows:
            name = str(row.filename)
            if name in _DOT_ENTRIES:
                continue
            if not _is_plain_name(name):
                _LOGGER.warning(
                    "Ignoring entry in %r: %r is not a filename a share can hold",
                    clean,
                    name,
                )
                continue
            entries.append(ShareEntry._from_shared_file(row, clean))
        entries.sort(key=lambda entry: (not entry.is_directory, entry.name.casefold()))
        return entries

    async def async_walk(self, path: str | PurePosixPath = "") -> list[ShareEntry]:
        """List a directory and everything beneath it.

        Directories appear before their contents. Costs one round trip per directory, so
        it is a real walk over the network rather than a local scan.
        """
        found: list[ShareEntry] = []
        pending = [_clean(path)]
        while pending:
            for entry in await self.async_list(pending.pop(0)):
                found.append(entry)
                if entry.is_directory:
                    pending.append(entry.path)
        return found

    async def async_stat(self, path: str | PurePosixPath) -> ShareEntry:
        """Fetch one path's metadata.

        :raises DisklavierShareNotFoundError: nothing is at that path.
        """
        clean = _clean(path)
        attributes = await self._call(
            "stat",
            clean,
            lambda c: c.getAttributes(
                self._share, _wire(clean), timeout=self._deadline
            ),
        )
        return ShareEntry(
            # getAttributes answers about the path it was asked about, and does not always
            # echo a filename back, so the name comes from the request.
            name=clean.rpartition("/")[2],
            path=clean,
            is_directory=bool(attributes.isDirectory),
            size=int(attributes.file_size),
            modified=_timestamp(float(attributes.last_write_time)),
        )

    async def async_exists(self, path: str | PurePosixPath) -> bool:
        """Whether anything is at that path."""
        clean = _clean(path)

        def probe(connection: SMBBackend) -> bool:
            # Settled inside the worker thread rather than by catching the translated
            # exception outside it. "Nothing there" is this question's ordinary answer,
            # not a fault, and building an exception to carry it -- then unwinding it
            # across the thread boundary and the await -- is ceremony for the common case.
            try:
                connection.getAttributes(
                    self._share, _wire(clean), timeout=self._deadline
                )
            except OperationFailure as err:
                if _status_of(err) in _STATUS_NOT_FOUND:
                    return False
                raise
            return True

        return await self._call("stat", clean, probe)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def _create_directory(self, path: str) -> bool:
        """Create one directory, reporting whether it had to be created.

        A name collision means the directory is already there, which is the outcome the
        caller wanted, so it is answered with ``False`` rather than raised. Letting the
        server say so costs one round trip where checking first would cost two.
        """

        def create(connection: SMBBackend) -> bool:
            # Handled in the worker thread, for the reason given in async_exists.
            try:
                connection.createDirectory(
                    self._share, _wire(path), timeout=self._deadline
                )
            except OperationFailure as err:
                if _status_of(err) in _STATUS_EXISTS:
                    return False
                raise
            return True

        return await self._call("create directory", path, create)

    async def async_makedirs(self, path: str | PurePosixPath) -> list[str]:
        """Create a directory and any missing parents.

        Existing directories are left alone, so this is safe to call repeatedly.

        :returns: The directories actually created, shallowest first.
        """
        clean = _clean(path)
        created: list[str] = []
        for candidate in [*_parents(clean), clean]:
            if candidate and await self._create_directory(candidate):
                created.append(candidate)
        return created

    async def async_upload(
        self,
        local_path: Path | str,
        remote_path: str | PurePosixPath,
        *,
        make_parents: bool = True,
    ) -> int:
        """Copy a local file onto the share, replacing whatever is there.

        :param make_parents: Create missing parent directories first.
        :returns: The number of bytes written.
        :raises OSError: the local file could not be read. Reported as itself -- a
            ``FileNotFoundError`` or ``PermissionError`` -- rather than as a share failure.
        """
        source = Path(local_path)
        clean = _clean(remote_path)
        if make_parents:
            await self._make_parents(clean)

        def store(connection: SMBBackend) -> int:
            # Opened inside the operation so a reconnect-and-retry rewinds to the start of
            # the file rather than resuming from a half-consumed handle.
            try:
                handle = source.open("rb")
            except OSError as err:
                raise _LocalFileError(err) from err
            with handle:
                return connection.storeFile(
                    self._share, _wire(clean), handle, timeout=self._deadline
                )

        return await self._call_local("upload", clean, store)

    async def async_upload_bytes(
        self,
        data: bytes,
        remote_path: str | PurePosixPath,
        *,
        make_parents: bool = True,
    ) -> int:
        """Write bytes to a file on the share, replacing whatever is there."""
        clean = _clean(remote_path)
        if make_parents:
            await self._make_parents(clean)

        def store(connection: SMBBackend) -> int:
            return connection.storeFile(
                self._share, _wire(clean), BytesIO(data), timeout=self._deadline
            )

        return await self._call("upload", clean, store)

    async def _make_parents(self, clean: str) -> None:
        """Create the directories leading to a file, if any are missing."""
        parent = clean.rpartition("/")[0]
        if parent:
            await self.async_makedirs(parent)

    async def async_download(
        self, remote_path: str | PurePosixPath, local_path: Path | str
    ) -> int:
        """Copy a file off the share to a local path.

        The transfer lands on a neighbouring temporary file and is moved into place once
        complete, so an interrupted download cannot leave a truncated file where a whole
        one is expected.

        :returns: The number of bytes read.
        :raises OSError: the local destination could not be written.
        """
        clean = _clean(remote_path)
        destination = Path(local_path)
        partial = destination.with_name(f"{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)

        def retrieve(connection: SMBBackend) -> int:
            try:
                handle = partial.open("wb")
            except OSError as err:
                raise _LocalFileError(err) from err
            with handle:
                _, size = connection.retrieveFile(
                    self._share, _wire(clean), handle, timeout=self._deadline
                )
            return int(size)

        try:
            size = await self._call_local("download", clean, retrieve)
        except BaseException:
            await asyncio.to_thread(partial.unlink, True)
            raise
        await asyncio.to_thread(partial.replace, destination)
        return size

    async def async_download_bytes(self, remote_path: str | PurePosixPath) -> bytes:
        """Read a file off the share into memory.

        The whole file is buffered, so prefer :meth:`async_download` for anything large.
        """
        clean = _clean(remote_path)

        def retrieve(connection: SMBBackend) -> bytes:
            buffer = BytesIO()
            connection.retrieveFile(
                self._share, _wire(clean), buffer, timeout=self._deadline
            )
            return buffer.getvalue()

        return await self._call("download", clean, retrieve)

    async def async_delete(self, remote_path: str | PurePosixPath) -> None:
        """Delete a file.

        :raises DisklavierShareNotFoundError: there is no such file.
        """
        clean = _clean(remote_path)
        await self._call(
            "delete",
            clean,
            lambda c: c.deleteFiles(self._share, _wire(clean), timeout=self._deadline),
        )

    async def async_remove_directory(self, remote_path: str | PurePosixPath) -> None:
        """Remove an empty directory.

        :raises DisklavierShareError: the directory still has contents. Use
            :meth:`async_delete_tree` to remove those too.
        """
        clean = _clean(remote_path)
        await self._call(
            "remove directory",
            clean,
            lambda c: c.deleteDirectory(
                self._share, _wire(clean), timeout=self._deadline
            ),
        )

    async def async_delete_tree(self, remote_path: str | PurePosixPath) -> list[str]:
        """Delete a directory and everything inside it.

        A path that turns out to be a file is simply deleted. Walking it first would ask the
        server to list a file, which answers "no such path" -- a flatly untrue error for
        something that is sitting right there.

        :returns: Every path removed, deepest first.
        :raises ValueError: the path is the share root. Emptying an entire share is never
            what a caller meant to ask for by leaving an argument blank.
        :raises DisklavierShareNotFoundError: there is genuinely nothing at that path.
        """
        clean = _clean(remote_path)
        if not clean:
            raise ValueError("refusing to delete the share root")

        if not (await self.async_stat(clean)).is_directory:
            await self.async_delete(clean)
            return [clean]

        entries = await self.async_walk(clean)
        removed: list[str] = []
        # Deepest first, so a directory is always empty by the time it is removed.
        for entry in sorted(entries, key=lambda e: e.path.count("/"), reverse=True):
            if entry.is_directory:
                await self.async_remove_directory(entry.path)
            else:
                await self.async_delete(entry.path)
            removed.append(entry.path)
        await self.async_remove_directory(clean)
        removed.append(clean)
        return removed

    async def async_rename(
        self, source: str | PurePosixPath, destination: str | PurePosixPath
    ) -> None:
        """Rename or move a file or directory within the share."""
        old = _clean(source)
        new = _clean(destination)
        await self._call(
            "rename",
            old,
            lambda c: c.rename(
                self._share, _wire(old), _wire(new), timeout=self._deadline
            ),
        )

    # ------------------------------------------------------------------
    # Mirroring a local directory
    # ------------------------------------------------------------------

    async def async_sync_directory(
        self,
        local_dir: Path | str,
        remote_dir: str | PurePosixPath = "",
        *,
        suffixes: Collection[str] | None = None,
        exclude: Collection[str] = DEFAULT_EXCLUDES,
        include: Callable[[str], bool] | None = None,
        prune: bool = False,
        dry_run: bool = False,
        continue_on_error: bool = False,
        progress: Callable[[SyncProgress], None] | None = None,
    ) -> SyncResult:
        """Mirror a local directory tree onto the share.

        A file is uploaded when the share has nothing at that path, when the sizes differ,
        or when the local file has been modified since the copy on the share was written.
        Everything else is skipped, which makes repeat runs cheap and interrupted runs
        resumable.

        Transfers run one at a time: a single SMB session carries one request, so there is
        nothing to be gained by launching them together.

        **Mind the depth.** The piano's indexer descends only
        :data:`~aiodisklavier.const.INDEXED_DEPTH_LIMIT` folder levels, so
        ``<folder>/<subfolder>/song.mid`` is the deepest path it will ever list. Anything
        below that copies without complaint and never appears in the library; this method
        logs a warning when it happens, but cannot flatten a tree on the caller's behalf.

        :param local_dir: The directory to mirror.
        :param remote_dir: Where to put it. The default is the share root.
        :param suffixes: Only upload files with these extensions, matched without regard to
            case. :data:`~aiodisklavier.const.PLAYABLE_SUFFIXES` is the set to reach for --
            note that it includes audio, because a ``song.wav`` or ``song.mp3`` beside a
            ``song.mid`` is that song's backing track rather than a separate song. Narrowing
            this to ``{".mid"}`` leaves every transcription playing bare; a warning is
            logged when that happens.
        :param exclude: Glob patterns matched against each path component. The default,
            :data:`~aiodisklavier.const.DEFAULT_EXCLUDES`, keeps macOS AppleDouble stubs
            off the share -- the firmware indexes those as songs and silently resets the
            piano's selection when one is loaded.
        :param include: Further filter, called with each path relative to ``local_dir``.
        :param prune: Delete files and directories on the share that the local tree does
            not have. Off by default: this deletes data.
        :param dry_run: Work out what would happen and report it, changing nothing.
        :param continue_on_error: Carry on past a file that fails, collecting it into
            :attr:`SyncResult.failed` instead of raising. Worth setting for a large tree,
            where one unreadable file should not undo an hour of transfers.
        :param progress: Called as each step happens, once per planned step, so its
            ``index`` always reaches ``total``. It runs on the event loop, so it must not
            block -- and an exception from it aborts the sync part-way, since swallowing a
            caller's bug silently would be worse than stopping.
        :raises ValueError: ``local_dir`` is not a directory.
        :raises OSError: a local file could not be read, unless ``continue_on_error``.

        Two things this deliberately does not do. It does not coordinate with *another*
        sync running on the same client: the lock serialises individual operations, not
        whole runs, so two overlapping syncs of the same tree each plan against the state
        they found and both transfer everything. And it compares remote paths exactly, so a
        file renamed only in its capitalisation is seen as new and sent again -- harmless on
        a case-insensitive server, which simply overwrites, but it will not settle down.
        """
        root = Path(local_dir)
        target = _clean(remote_dir)

        files = await asyncio.to_thread(_scan_local, root, exclude, suffixes, include)
        remote = await self._remote_index(target)
        plan = _plan_sync(files, target, remote, prune=prune)
        _warn_if_too_deep(plan)

        if dry_run:
            return SyncResult(
                uploaded=tuple(path for _, path in plan.uploads),
                skipped=tuple(plan.skipped),
                removed=tuple(entry.path for entry in plan.removals),
                directories=tuple(plan.directories),
                bytes_uploaded=sum(local.size for local, _ in plan.uploads),
            )
        return await self._apply_sync(
            plan, progress, continue_on_error=continue_on_error
        )

    async def _remote_index(self, target: str) -> dict[str, ShareEntry]:
        """Snapshot the destination subtree, keyed by share-relative path.

        One walk beats a stat per file: the catalogue this was built for is well over a
        thousand files, and a round trip each would dominate the run.
        """
        if target and not await self.async_exists(target):
            return {}
        return {entry.path: entry for entry in await self.async_walk(target)}

    async def _apply_sync(
        self,
        plan: _SyncPlan,
        progress: Callable[[SyncProgress], None] | None,
        *,
        continue_on_error: bool,
    ) -> SyncResult:
        """Carry out a planned sync."""
        total = plan.steps
        step = 0

        def report(action: SyncAction, path: str, size: int) -> None:
            nonlocal step
            step += 1
            if progress is not None:
                progress(SyncProgress(action, path, size, step, total))

        directories: list[str] = []
        uploaded: list[str] = []
        removed: list[str] = []
        failed: list[SyncFailure] = []
        written = 0

        for directory in plan.directories:
            # One call each rather than async_makedirs: the plan already lists every
            # missing ancestor, shallowest first, so the parents are handled by the time
            # their children come up.
            #
            # The plan can only see inside the destination, so directories *above* it look
            # missing when they are not. What the server says is authoritative, and only a
            # directory this run actually created is reported as one -- otherwise a
            # repeated sync would report work it did not do, and `changed` would ask for a
            # reindex that nothing needs.
            if await self._create_directory(directory):
                directories.append(directory)
                report(SyncAction.CREATE_DIRECTORY, directory, 0)
            else:
                report(SyncAction.SKIP, directory, 0)

        for local, destination in plan.uploads:
            try:
                written += await self.async_upload(
                    local.source, destination, make_parents=False
                )
            except (DisklavierShareError, DisklavierConnectionError, OSError) as err:
                if not continue_on_error:
                    raise
                _LOGGER.warning("Could not upload %s: %s", destination, err)
                failed.append(SyncFailure(destination, str(err)))
                # Still a step. Without this the progress stream stops short of its total,
                # so a bar driven by it sticks below 100% for the rest of the run.
                report(SyncAction.FAIL, destination, local.size)
                continue
            uploaded.append(destination)
            report(SyncAction.UPLOAD, destination, local.size)

        for path in plan.skipped:
            report(SyncAction.SKIP, path, 0)

        for entry in plan.removals:
            if entry.is_directory:
                await self.async_remove_directory(entry.path)
            else:
                await self.async_delete(entry.path)
            removed.append(entry.path)
            report(SyncAction.REMOVE, entry.path, entry.size)

        return SyncResult(
            uploaded=tuple(uploaded),
            skipped=tuple(plan.skipped),
            removed=tuple(removed),
            directories=tuple(directories),
            failed=tuple(failed),
            bytes_uploaded=written,
        )


# ----------------------------------------------------------------------
# Sync planning
# ----------------------------------------------------------------------


def _scan_local(
    root: Path,
    exclude: Collection[str],
    suffixes: Collection[str] | None,
    include: Callable[[str], bool] | None,
) -> list[_LocalFile]:
    """Walk a local directory, applying the filters. Blocking; runs in a worker thread."""
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    wanted = None if suffixes is None else {suffix.casefold() for suffix in suffixes}
    found: list[_LocalFile] = []
    # Audio the filters rejected, by the path it would have gone to. Used to spot backing
    # tracks left behind, which is a silent fault: the MIDI still plays, just bare.
    rejected_audio: set[str] = set()
    for path in _iter_files(root, exclude):
        relative = path.relative_to(root).as_posix()
        wrong_suffix = wanted is not None and path.suffix.casefold() not in wanted
        excluded = include is not None and not include(relative)
        if wrong_suffix or excluded:
            if path.suffix.casefold() in AUDIO_SUFFIXES:
                rejected_audio.add(relative.rpartition(".")[0])
            continue
        stat = path.stat()
        found.append(
            _LocalFile(
                source=path,
                relative=relative,
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
    _warn_if_audio_dropped(found, rejected_audio)
    found.sort(key=lambda local: local.relative)
    return found


def _iter_files(root: Path, exclude: Collection[str]) -> Iterator[Path]:
    """Yield every file under ``root`` whose path clears the exclude patterns.

    Excluding a directory excludes what is under it: the patterns are tested against every
    component, so nothing inside a pruned directory can slip through.
    """
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(_matches(part, exclude) for part in relative.parts):
            continue
        # Symlinks are resolved by is_file(); a dangling one is simply not a file.
        if path.is_file():
            yield path


def _warn_if_audio_dropped(files: list[_LocalFile], rejected_audio: set[str]) -> None:
    """Warn when a MIDI file is being sent without the backing track sitting beside it.

    The piano pairs ``song.mid`` with ``song.wav`` or ``song.mp3`` and plays them together.
    Filtering a mirror down to ``{".mid"}`` therefore produces a sync that works, indexes,
    and plays -- as a bare piano part, with the band missing. Nothing anywhere reports it,
    so it is worth a line in the log.
    """
    orphaned = sorted(
        local.relative
        for local in files
        if local.source.suffix.casefold() not in AUDIO_SUFFIXES
        and local.relative.rpartition(".")[0] in rejected_audio
    )
    if not orphaned:
        return
    _LOGGER.warning(
        "%d file(s) have a backing audio track that the filters excluded, e.g. %s. They "
        "will play as bare MIDI. Include %s in `suffixes` to send the audio too.",
        len(orphaned),
        orphaned[0],
        "/".join(sorted(AUDIO_SUFFIXES)),
    )


def _warn_if_too_deep(plan: _SyncPlan) -> None:
    """Warn about files the piano's indexer will never see.

    Nothing about the copy fails: the file lands, lists over SMB, and is simply absent from
    the library afterwards, with no error from the write or the reindex. That silence is the
    problem, so it is broken here -- a deep tree is the natural way to lay a music library
    out, and the shape the firmware wants is not obvious.
    """
    too_deep = [
        destination
        for _, destination in plan.uploads
        if destination.count("/") > INDEXED_DEPTH_LIMIT
    ]
    if not too_deep:
        return
    _LOGGER.warning(
        "%d file(s) sit deeper than the %d folder levels the piano indexes and will not "
        "appear in its library, e.g. %s. Flatten the tree to <folder>/<subfolder>/song.mid.",
        len(too_deep),
        INDEXED_DEPTH_LIMIT,
        too_deep[0],
    )


def _needs_upload(local: _LocalFile, remote: ShareEntry | None) -> bool:
    """Decide whether a local file has to be sent.

    The share stores the time of the copy, not the source file's own timestamp, so a local
    file newer than its copy is one that changed after it was last sent.
    """
    if remote is None or remote.is_directory:
        return True
    if remote.size != local.size:
        return True
    return local.mtime > remote.modified.timestamp() + MTIME_TOLERANCE


def _plan_sync(
    files: Iterable[_LocalFile],
    target: str,
    remote: dict[str, ShareEntry],
    *,
    prune: bool,
) -> _SyncPlan:
    """Work out the mutations a sync needs, without performing any of them."""
    plan = _SyncPlan()
    wanted: set[str] = set()
    # Every directory the mirrored tree needs, whether or not it is there already. Pruning
    # reads this set too, so it must describe the finished state rather than only the work
    # -- listing just the missing ones would have prune delete the directories that exist.
    needed: set[str] = {target} if target else set()

    for local in files:
        destination = _join(target, local.relative)
        wanted.add(destination)
        needed.update(_parents(destination))
        if _needs_upload(local, remote.get(destination)):
            plan.uploads.append((local, destination))
        else:
            plan.skipped.append(destination)

    # Shallowest first, so every parent exists before its children.
    plan.directories = sorted(
        (path for path in needed if path not in remote),
        key=lambda path: (path.count("/"), path),
    )

    if prune:
        keep = wanted | needed
        # Deepest first, so a directory is empty by the time it is removed.
        plan.removals = sorted(
            (entry for path, entry in remote.items() if path not in keep),
            key=lambda entry: (-entry.path.count("/"), entry.path),
        )

    return plan
