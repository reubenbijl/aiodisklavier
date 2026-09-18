"""Describe a piano for a bug report: ``python -m aiodisklavier <host>``.

This library was written against one instrument, so the most useful thing another owner can
send is what *their* piano says. This prints that, in a form made to be pasted into an
issue -- and made to be safe to paste:

- **It only reads.** Nothing here changes the piano's state or makes a sound. In particular
  it never asks for the radio channel list, which looks like a read and stops whatever the
  piano is playing.
- **It reports shapes, not contents.** For the state files it prints which keys exist and
  what type each value is. Actual values are printed only for a short list of fields whose
  whole point is their vocabulary -- a status, a mode -- because a value this library has
  never seen is exactly what the report is for. Everything else stays out: ``master.json``
  carries the account's email address and the login passcode, ``settings.json`` the network
  configuration, and no song title, file name or id is printed at all.

Private: run it as a module. Nothing in here is API.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from functools import partial
from typing import Any, Final

import aiohttp

from .client import Disklavier
from .const import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    ISSUES_URL,
    MAX_SONG_DB_BYTES,
    PATH_CTRL_MASTER_JSON,
    PATH_CTRL_SONG_DB,
    PATH_CURRENT_INFO,
    PATH_STATIC_INFO,
    PREFIX_TO_SONG_GROUP,
    PlaybackStatus,
    PlaylistGroup,
    PowerStatus,
    QuietMode,
    RepeatMode,
    SongFormat,
    SongGroup,
)
from .exceptions import DisklavierEnvelopeError, DisklavierError
from .models import _dict_or_empty
from .share import DisklavierShare, SMBBackend

#: ``static_info`` fields that identify a model and firmware rather than an instrument.
_IDENTITY_FIELDS: Final = (
    "enspire_model",
    "piano_type",
    "enspire_region",
    "enspire_version",
    "api_version",
    "api_revision",
)

#: ``current_info`` fields worth their values, with the vocabulary this library knows.
_CURRENT_INFO_ENUMS: Final[Mapping[str, type[StrEnum]]] = {
    "power_status": PowerStatus,
    "quiet_status": QuietMode,
    "playback_status": PlaybackStatus,
}

#: ``master.json`` fields worth their values, as dotted paths. Every one is a status or a
#: mode. Anything not listed here is reported by type only -- this is an allow-list because
#: the file also holds an email address and a passcode, and a deny-list would leak the
#: first sensitive field a future firmware adds.
_MASTER_VALUES: Final = frozenset(
    {
        "repeat",
        "seq.status",
        "seq.song_pfix",
        "seq.sync",
        "seq.skip_space",
        "piano.quiet",
        "piano.met_status",
        "piano.key_motion",
        "sbc.headphone",
        "sbc.usb",
        "radio.info",
        "radio.status",
        "rcs.status",
        "rcs.mode",
        "mute.status",
        "msgbox.type",
        "ab_repeat.status",
        "apictrl.exclusive_mode",
        "apictrl.wait_ctrl",
    }
)

#: How deep to describe a state file. The interesting structure is in the first two levels.
_MAX_DEPTH: Final = 3

#: Clip any printed value to this many characters. They come from the device.
_MAX_VALUE_CHARS: Final = 40


def _show(value: Any) -> str:
    """Render a device-supplied value safely: repr'd, so control characters stay inert."""
    text = str(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + "..."
    return repr(text)


def _verdict(enum: type[StrEnum], value: Any) -> str:
    """Say whether this library has a name for a value."""
    if value is None or value == "":
        return "absent"
    try:
        enum(value)
    except ValueError:
        return "NOT RECOGNISED"
    return "known"


def _shape(value: Any, path: str, depth: int, lines: list[str]) -> None:
    """Describe a JSON value by structure, printing values only where allow-listed."""
    indent = "  " * depth
    name = path.rpartition(".")[2]
    if isinstance(value, dict) and depth < _MAX_DEPTH:
        lines.append(f"{indent}- {name}: object, {len(value)} keys")
        for key in sorted(value):
            _shape(value[key], f"{path}.{key}", depth + 1, lines)
        return
    kind = type(value).__name__
    if path.partition(".")[2] in _MASTER_VALUES:
        lines.append(f"{indent}- {name}: {kind} = {_show(value)}")
    else:
        lines.append(f"{indent}- {name}: {kind}")


def _tally(rows: Mapping[str, Any], field: str) -> Counter[str]:
    """Count the values one field takes across a database section's rows."""
    return Counter(
        str(row.get(field)) if isinstance(row, dict) else "?" for row in rows.values()
    )


def _describe_tally(
    title: str, counts: Counter[str], known: Callable[[str], bool]
) -> list[str]:
    """Render a tally, flagging the values this library has no name for."""
    lines = [f"- {title}:"]
    for value, count in sorted(counts.items()):
        flag = "" if known(value) else "   <-- NOT RECOGNISED"
        lines.append(f"  - {_show(value)}: {count}{flag}")
    return lines


async def _section(title: str, lines: list[str], body: Callable[[], Any]) -> None:
    """Run one part of the report, recording a failure instead of stopping on it.

    A piano that lacks an endpoint is itself a finding, and the rest of the report is
    still worth having.
    """
    lines += ["", f"## {title}"]
    try:
        lines += await body()
    except (DisklavierError, OSError) as err:
        lines.append(f"- could not read: {type(err).__name__}: {_show(err)}")


async def probe(
    host: str,
    session: aiohttp.ClientSession,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    share: bool = True,
    connection_factory: Callable[[], SMBBackend] | None = None,
) -> str:
    """Build the report. Read-only, silent, and free of anything identifying."""
    from . import __version__

    piano = Disklavier(host, session, port=port, timeout=timeout)
    lines = [
        f"# aiodisklavier {__version__} probe",
        "",
        f"Python {platform.python_version()} on {platform.system()}. Read-only: nothing",
        "here changed the piano's state. Shapes and vocabularies only -- no titles, ids,",
        f"addresses or account details. Please paste it into {ISSUES_URL}",
    ]

    async def identity() -> list[str]:
        data = await piano._get_json(PATH_STATIC_INFO)
        found = [
            f"- {key}: {_show(data[key])}" for key in _IDENTITY_FIELDS if key in data
        ]
        missing = [key for key in _IDENTITY_FIELDS if key not in data]
        extra = sorted(set(data) - set(_IDENTITY_FIELDS) - {"disklavier_id"})
        return [
            *found,
            f"- disklavier_id: {'present, not shown' if 'disklavier_id' in data else 'ABSENT'}",
            f"- missing fields: {missing or 'none'}",
            f"- fields this library does not read: {extra or 'none'}",
        ]

    async def current_info() -> list[str]:
        data = await piano._get_json(PATH_CURRENT_INFO)
        found = []
        for key in sorted(data):
            enum = _CURRENT_INFO_ENUMS.get(key)
            if enum is None:
                found.append(f"- {key}: {type(data[key]).__name__}")
            else:
                found.append(
                    f"- {key}: {_show(data[key])} ({_verdict(enum, data[key])})"
                )
        missing = sorted(set(_CURRENT_INFO_ENUMS) - set(data))
        return [*found, f"- missing status fields: {missing or 'none'}"]

    async def master() -> list[str]:
        data = await piano._get_json(PATH_CTRL_MASTER_JSON)
        found: list[str] = []
        for key in sorted(data):
            _shape(data[key], f"master.{key}", 0, found)
        return [
            *found,
            f"- repeat verdict: {_verdict(RepeatMode, data.get('repeat'))}",
        ]

    async def song_db() -> list[str]:
        data = await piano._get_json(PATH_CTRL_SONG_DB, max_bytes=MAX_SONG_DB_BYTES)
        sections = {
            key: len(value) if isinstance(value, dict | list) else type(value).__name__
            for key, value in sorted(data.items())
        }
        songs = _dict_or_empty(data.get("song"))
        albums = _dict_or_empty(data.get("album"))
        row = next((r for r in songs.values() if isinstance(r, dict)), {})
        return [
            f"- sections: {sections}",
            f"- song row fields: {sorted(row)}",
            *_describe_tally(
                "song prefixes",
                _tally(songs, "pfix"),
                PREFIX_TO_SONG_GROUP.__contains__,
            ),
            *_describe_tally(
                "album prefixes",
                _tally(albums, "pfix"),
                PREFIX_TO_SONG_GROUP.__contains__,
            ),
            *_describe_tally(
                "formats",
                _tally(songs, "format"),
                lambda value: _verdict(SongFormat, value) == "known",
            ),
        ]

    async def libraries() -> list[str]:
        found = []
        for group in SongGroup:
            songs = await _count(partial(piano.async_get_songs, group))
            albums = await _count(partial(piano.async_get_albums, group))
            found.append(f"- {group.value}: songs {songs}, albums {albums}")
        for playlist_group in PlaylistGroup:
            playlists = await _count(partial(piano.async_get_playlists, playlist_group))
            found.append(f"- {playlist_group.value}: playlists {playlists}")
        return found

    async def shares() -> list[str]:
        async with DisklavierShare(
            host, timeout=max(timeout, 1.0), connection_factory=connection_factory
        ) as client:
            names = await client.async_list_shares()
            entries = await client.async_list()
        directories = sum(1 for entry in entries if entry.is_directory)
        return [
            "- guest session: accepted",
            f"- shares: {', '.join(sorted(_show(name) for name in names))}",
            f"- share root: {directories} folders, {len(entries) - directories} files",
        ]

    await _section("Identity (static_info)", lines, identity)
    await _section("Live state (current_info)", lines, current_info)
    await _section("Extended state (master.json), by shape", lines, master)
    await _section("Song database (song.json)", lines, song_db)
    await _section("Libraries, by count", lines, libraries)
    if share:
        await _section("SMB share", lines, shares)
    return "\n".join(lines) + "\n"


async def _count(listing: Callable[[], Any]) -> str:
    """Count a listing, or say why the piano would not give one."""
    try:
        return str(len(await listing()))
    except DisklavierEnvelopeError as err:
        return f"declined ({_show(err.error_info)})"
    except DisklavierError as err:
        return f"failed ({type(err).__name__})"


async def _run(args: argparse.Namespace) -> str:
    """Open a session and build the report."""
    async with aiohttp.ClientSession() as session:
        return await probe(
            args.host,
            session,
            port=args.port,
            timeout=args.timeout,
            share=not args.no_share,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, print the report, and return an exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m aiodisklavier",
        description=(
            "Describe a Disklavier ENSPIRE for a bug report. Read-only and silent; "
            "prints shapes and vocabularies, never titles, ids or account details."
        ),
    )
    parser.add_argument("host", help="the piano's hostname or IP address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request"
    )
    parser.add_argument("--no-share", action="store_true", help="skip the SMB share")
    args = parser.parse_args(argv)
    sys.stdout.write(asyncio.run(_run(args)))
    return 0
