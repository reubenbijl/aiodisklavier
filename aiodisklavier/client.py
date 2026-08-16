"""Async client for the Yamaha Disklavier ENSPIRE local HTTP API.

The piano exposes two HTTP interfaces. This client prefers the versioned open API under
``/api/`` and falls back to the internal ``/ctrl/`` endpoints only for the handful of
capabilities the open API omits -- seeking, repeat and shuffle, the extended state
block, library reindexing, and the test chord.

Example::

    async with aiohttp.ClientSession() as session:
        piano = Disklavier("192.168.1.50", session)
        info = await piano.async_get_current_info()
        if not info.is_playing:
            await piano.async_play()
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Any, Final

import aiohttp
from yarl import URL

from .const import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    JSON_RETRY_ATTEMPTS,
    JSON_RETRY_DELAY,
    MAX_RESPONSE_BYTES,
    MAX_SONG_DB_BYTES,
    NOTIFY_POLL_INTERVAL,
    NOTIFY_SETTLE,
    NOTIFY_WAIT_TIMEOUT,
    PATH_API_BASE,
    PATH_CTRL_MASTER_JSON,
    PATH_CTRL_PUT_NOTE_ON,
    PATH_CTRL_REFRESH_DB,
    PATH_CTRL_SEQ,
    PATH_CTRL_SONG,
    PATH_CTRL_SONG_DB,
    PATH_CURRENT_INFO,
    PATH_STATIC_INFO,
    PREFIX_TO_SONG_GROUP,
    VOLUME_MAX,
    VOLUME_MIN,
    Genre,
    GenreSelect,
    PlaylistGroup,
    PowerStatus,
    QuietMode,
    RepeatMode,
    SearchKind,
    SongGroup,
)
from .exceptions import (
    DisklavierCommandError,
    DisklavierConnectionError,
    DisklavierError,
    DisklavierResponseError,
)
from .models import (
    Album,
    CurrentInfo,
    LibrarySong,
    MasterState,
    PlaybackSnapshot,
    Playlist,
    RadioChannel,
    SearchResult,
    Song,
    SongDatabase,
    StaticInfo,
)

_LOGGER = logging.getLogger(__name__)

#: Sentinel for the piano's valueless flag arguments, e.g. ``/api/1.0/set_power_status?sleep``.
_FLAG: Final = ""

#: List responses use a different key depending on the group being queried; a playlist-ish
#: group returns ``item_list`` where a song group returns ``song_list``.
_SONG_KEYS: Final = ("song_list", "item_list")

#: How much to pull off the wire per read while enforcing the response-size ceiling.
_READ_CHUNK_BYTES: Final = 64 * 1024

#: ``error_info`` value the firmware uses for an empty library: HTTP 200 carrying
#: ``{"status": "error", "error_info": "no song", "song_list": []}``. A routine browse
#: result, not a fault, so the browse methods translate it back into the empty list.
_EMPTY_LIBRARY_ERROR: Final = "no song"

#: The daemon writes its socket payloads with a trailing ``\n\0`` terminator, and that
#: terminator sometimes survives into the state-file responses. ``json.loads`` rejects the
#: trailing NUL as extra data, so it is stripped -- along with ordinary whitespace -- before
#: parsing. This is distinct from a genuinely truncated read, which still retries.
_JSON_STRIP: Final = "\x00 \t\r\n\v\f"

#: Fuzzy matches scoring below this are dropped from search results. difflib happily
#: reports similarity between any two strings; below here it is noise, not a match.
_FUZZY_CUTOFF: Final = 0.6


def _match_score(query: str, title: str) -> float:
    """Score a title against a query: 0 is no match, 1 is exact.

    Exact match beats a prefix, a prefix beats a substring, and a substring beats a
    fuzzy resemblance -- so "Clair" surfaces "Clair de lune" ahead of titles that merely
    look similar. Comparison is casefolded, and fuzzy scores below
    :data:`_FUZZY_CUTOFF` are treated as no match at all.
    """
    wanted = query.casefold().strip()
    candidate = title.casefold()
    if not wanted or not candidate:
        return 0.0
    if wanted == candidate:
        return 1.0
    ratio = difflib.SequenceMatcher(None, wanted, candidate).ratio()
    if candidate.startswith(wanted):
        return max(0.9, ratio)
    if wanted in candidate:
        return max(0.8, ratio)
    return ratio if ratio >= _FUZZY_CUTOFF else 0.0


class Disklavier:
    """Client for a single Disklavier ENSPIRE.

    :param host: Hostname or IP address of the piano.
    :param session: An ``aiohttp`` session owned by the caller.
    :param port: HTTP port; the piano only ever serves on 80.
    :param timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialise the client."""
        self._host = host
        self._port = port
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._base = URL.build(scheme="http", host=host, port=port)
        self._song_db: SongDatabase | None = None

    @property
    def host(self) -> str:
        """The piano's host."""
        return self._host

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> bytes:
        """Perform a GET and return the raw body.

        The body is returned undecoded: a state read can catch the daemon mid-write, and
        a cut that lands inside a multibyte character must surface at the JSON layer --
        where truncation is retried -- rather than escape from here as a stray
        ``UnicodeDecodeError``.

        :raises DisklavierCommandError: the piano returned HTTP 400.
        :raises DisklavierConnectionError: the piano was unreachable or timed out.
        :raises DisklavierResponseError: the piano redirected, or the body ran past
            ``max_bytes`` (:data:`~aiodisklavier.const.MAX_RESPONSE_BYTES` by default).
        """
        url = self._base.with_path(path)
        try:
            async with self._session.get(
                url, params=params, timeout=self._timeout, allow_redirects=False
            ) as response:
                # The firmware signals every bad argument as a plain 400.
                if response.status == 400:
                    raise DisklavierCommandError(
                        f"Disklavier rejected request {url} with params {params}"
                    )
                # Nothing this client calls ever redirects, and following one would
                # hand the request to whatever host a spoofed piano names.
                if 300 <= response.status < 400:
                    raise DisklavierResponseError(
                        f"Disklavier redirected {url} unexpectedly ({response.status})"
                    )
                response.raise_for_status()
                # Read incrementally against a ceiling. A plain ``read()`` would buffer
                # whatever the device chooses to stream; real payloads top out around a
                # few hundred kB, so past the ceiling this is not the piano talking.
                body = bytearray()
                while chunk := await response.content.read(_READ_CHUNK_BYTES):
                    body += chunk
                    if len(body) > max_bytes:
                        raise DisklavierResponseError(
                            f"Disklavier response from {url} exceeded {max_bytes} bytes"
                        )
                return bytes(body)
        except DisklavierError:
            raise
        except TimeoutError as err:
            raise DisklavierConnectionError(
                f"Timeout connecting to Disklavier at {self._host}"
            ) from err
        except aiohttp.ClientError as err:
            raise DisklavierConnectionError(
                f"Error communicating with Disklavier at {self._host}: {err}"
            ) from err

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        """Perform a GET and decode the JSON body.

        The firmware serves JSON with a ``text/html`` content type in places, so the body is
        decoded explicitly rather than relying on ``response.json()``.
        """
        raw = b""
        for attempt in range(JSON_RETRY_ATTEMPTS):
            try:
                raw = await self._get(path, params, max_bytes=max_bytes)
                # Decode and parse under one net: a read that catches the daemon
                # mid-write can be cut inside a multibyte character just as easily as
                # inside the JSON, and both heal the same way -- by re-reading. A
                # ``UnicodeDecodeError`` is a ``ValueError``, so one handler covers
                # both. The strip removes the daemon's ``\n\0`` terminator (and any
                # stray whitespace), without which a complete payload is rejected for
                # the trailing NUL.
                data: Any = json.loads(raw.decode().strip(_JSON_STRIP))
            except ValueError:
                # The state endpoints are files the daemon rewrites in place, so a read
                # can catch one part-written. Retrying almost always gets a whole one.
                # The final attempt falls out of the loop to the raise below.
                if attempt < JSON_RETRY_ATTEMPTS - 1:
                    _LOGGER.debug(
                        "Truncated JSON from %s, re-reading (attempt %d)",
                        path,
                        attempt + 1,
                    )
                    await asyncio.sleep(JSON_RETRY_DELAY)
                continue
            if not isinstance(data, dict):
                raise DisklavierResponseError(
                    f"Disklavier returned unexpected JSON from {path}: {data!r}"
                )
            return data

        raise DisklavierResponseError(
            f"Disklavier returned invalid JSON from {path} after "
            f"{JSON_RETRY_ATTEMPTS} attempts ({len(raw)} bytes): {raw[:200]!r}..."
        )

    @staticmethod
    def _command_path(command: str) -> str:
        """Build the versioned open API path for a command."""
        return f"{PATH_API_BASE}/{command}"

    async def _command(self, command: str, **params: Any) -> None:
        """Send an open API command that returns no body worth parsing."""
        await self._get(self._command_path(command), params)

    async def _command_json(
        self, command: str, *, allow_empty: bool = False, **params: Any
    ) -> dict[str, Any]:
        """Send an open API command and decode its JSON envelope.

        :param allow_empty: Accept the firmware's empty-library envelope -- ``status:
            error`` with ``error_info: "no song"`` -- as a normal reply. An empty library
            is a routine browse result the firmware happens to spell as an error, and its
            envelope still carries the (empty) list keys.
        :raises DisklavierResponseError: the envelope carried any other ``status`` !=
            ``ok``. The exception's ``command`` and ``error_info`` attributes identify
            the failure without parsing the message.
        """
        data = await self._get_json(self._command_path(command), params)
        status = data.get("status")
        if status == "ok":
            return data
        error_info = data.get("error_info")
        if allow_empty and error_info == _EMPTY_LIBRARY_ERROR:
            return data
        raise DisklavierResponseError(
            f"Disklavier command {command!r} failed: "
            f"{error_info or status or 'unknown error'}",
            command=command,
            error_info=error_info if isinstance(error_info, str) else None,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    async def async_get_static_info(self) -> StaticInfo:
        """Fetch the piano's identity. Suitable for a config flow and device registry."""
        return StaticInfo.from_json(await self._get_json(PATH_STATIC_INFO))

    async def async_get_current_info(self) -> CurrentInfo:
        """Fetch live state. This is the only call a state poll needs."""
        return CurrentInfo.from_json(await self._get_json(PATH_CURRENT_INFO))

    async def async_get_master_state(self) -> MasterState:
        """Fetch the extended state the open API does not expose.

        Reads the internal ``/ctrl/master.json``. Use this only for what
        :meth:`async_get_current_info` lacks -- notably the repeat mode.
        """
        return MasterState.from_json(await self._get_json(PATH_CTRL_MASTER_JSON))

    # ------------------------------------------------------------------
    # Transport control
    # ------------------------------------------------------------------

    async def async_play(self) -> None:
        """Start or resume playback."""
        await self._command("play")

    async def async_pause(self) -> None:
        """Pause playback, keeping the current position."""
        await self._command("pause")

    async def async_stop(self) -> None:
        """Stop playback and rewind to the start.

        The piano has no distinct stopped state: afterwards ``playback_status`` reads
        ``pause`` with a position of zero.
        """
        await self._command("stop")

    async def async_play_pause(self) -> None:
        """Toggle between playing and paused."""
        await self._command("play_pause")

    async def async_next_song(self) -> None:
        """Advance to the next song."""
        await self._command("next_song")

    async def async_previous_song(self) -> None:
        """Go back to the previous song."""
        await self._command("prev_song")

    async def async_restart_song(self) -> None:
        """Restart the current song from the beginning."""
        await self._command("back_song")

    async def async_seek(self, position_ms: int) -> None:
        """Seek to an absolute position in milliseconds.

        Uses the internal ``setSeq.php`` endpoint; the open API cannot seek.
        """
        if position_ms < 0:
            raise ValueError("position_ms must not be negative")
        await self._get(PATH_CTRL_SEQ, {"time": str(int(position_ms))})

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    async def async_set_volume(self, volume: int) -> None:
        """Set the main volume, 0-100.

        :raises ValueError: the value is outside the accepted range. The firmware would
            answer HTTP 400; this check keeps the failure local and legible.
        """
        if not VOLUME_MIN <= volume <= VOLUME_MAX:
            raise ValueError(
                f"volume must be between {VOLUME_MIN} and {VOLUME_MAX}, got {volume}"
            )
        await self._command("set_volume_main", _v=str(volume))

    async def async_volume_up(self) -> None:
        """Raise the main volume by one step of 10."""
        await self._command("volume_up_main")

    async def async_volume_down(self) -> None:
        """Lower the main volume by one step of 10."""
        await self._command("volume_down_main")

    # ------------------------------------------------------------------
    # Power and voicing
    # ------------------------------------------------------------------

    async def async_set_power(self, status: PowerStatus) -> None:
        """Wake the piano or send it to standby.

        Only :attr:`PowerStatus.ON` and :attr:`PowerStatus.SLEEP` may be requested;
        :attr:`PowerStatus.WAKEUP` is a transitional state the piano reports on its own.

        Waking takes roughly 12 seconds, during which ``power_status`` reads ``wakeup``.
        """
        if status is PowerStatus.WAKEUP:
            raise ValueError("WAKEUP is reported by the piano, it cannot be requested")
        # These are valueless flags: '?_com=set_power_status&sleep'.
        await self._command("set_power_status", **{status.value: _FLAG})

    async def async_turn_on(self) -> None:
        """Wake the piano from standby."""
        await self.async_set_power(PowerStatus.ON)

    async def async_turn_off(self) -> None:
        """Send the piano to standby."""
        await self.async_set_power(PowerStatus.SLEEP)

    async def async_set_quiet_mode(self, mode: QuietMode) -> None:
        """Choose whether the hammers physically strike the strings."""
        # Also valueless flags.
        await self._command("set_quiet_status", **{mode.value: _FLAG})

    async def async_set_repeat(self, mode: RepeatMode) -> None:
        """Set the repeat and shuffle mode.

        Uses the internal ``setSong.php`` endpoint; the open API has no equivalent.
        """
        await self._get(PATH_CTRL_SONG, {"repeat": mode.value})

    # ------------------------------------------------------------------
    # Choosing what to play
    # ------------------------------------------------------------------

    @staticmethod
    def _song_command(load_only: bool, single: bool) -> str:
        """Pick between cueing, playing, and playing exactly one song."""
        if load_only:
            return "load_song"
        return "play_single_song" if single else "play_song"

    async def async_play_song(
        self,
        song_id: int,
        group: SongGroup,
        *,
        load_only: bool = False,
        single: bool = False,
    ) -> None:
        """Play a song by id from a library.

        :param load_only: Cue the song without starting it.
        :param single: Play this song and stop, rather than continuing into the rest of
            the library. Ignored when ``load_only`` is set.
        """
        await self._command(
            self._song_command(load_only, single),
            id=str(song_id),
            group=group.value,
        )

    async def async_play_search(
        self, title: str, *, load_only: bool = False, single: bool = False
    ) -> None:
        """Play the best fuzzy match for a song title.

        The match runs on the piano, so partial titles work well -- "Clair" finds
        "Clair de lune".

        :param single: Play just this song and stop.
        """
        if not title:
            raise ValueError("title must not be empty")
        await self._command(self._song_command(load_only, single), search_title=title)

    async def async_play_genre(
        self,
        genre: Genre,
        *,
        select: GenreSelect = GenreSelect.TOP,
        load_only: bool = False,
    ) -> None:
        """Play from a genre folder of the built-in library."""
        await self._command(
            "load_song" if load_only else "play_song",
            group=SongGroup.BUILT_IN_SONGS.value,
            folder=genre.value,
            select=select.value,
        )

    async def async_play_album(
        self, album_id: int, group: SongGroup, *, load_only: bool = False
    ) -> None:
        """Play an album by id."""
        await self._command(
            "load_album" if load_only else "play_album",
            album_id=str(album_id),
            group=group.value,
        )

    async def async_play_playlist(
        self, playlist_id: int, group: PlaylistGroup, *, load_only: bool = False
    ) -> None:
        """Play a playlist by id."""
        await self._command(
            "load_playlist" if load_only else "play_playlist",
            playlist_id=str(playlist_id),
            group=group.value,
        )

    async def async_play_playlist_item(
        self, item_id: int, group: PlaylistGroup, *, load_only: bool = False
    ) -> None:
        """Play a single item from within a playlist."""
        await self._command(
            "load_playlist_item" if load_only else "play_playlist_item",
            item_id=str(item_id),
            group=group.value,
        )

    # ------------------------------------------------------------------
    # Browsing the libraries
    # ------------------------------------------------------------------

    @staticmethod
    def _songs_from(data: dict[str, Any]) -> list[Song]:
        """Read a song list, accepting either key the firmware might use.

        A null list is treated as the empty list it denotes, rather than iterated.
        """
        for key in _SONG_KEYS:
            if key in data:
                return [
                    Song(
                        song_id=int(row.get("song_id", row.get("item_id", 0))),
                        title=str(row.get("song_title", "")),
                    )
                    for row in data[key] or []
                ]
        return []

    async def async_get_songs(self, group: SongGroup) -> list[Song]:
        """List the songs in a library.

        An empty library returns an empty list. The firmware reports it as ``status:
        error`` with ``error_info: "no song"``; that envelope is translated back into
        the empty result it denotes rather than raised.
        """
        return self._songs_from(
            await self._command_json(
                "get_song_list", allow_empty=True, group=group.value
            )
        )

    async def async_get_albums(self, group: SongGroup) -> list[Album]:
        """List the albums in a library."""
        data = await self._command_json(
            "get_album_list", allow_empty=True, group=group.value
        )
        return [
            Album(
                album_id=int(row.get("album_id", 0)),
                title=str(row.get("album_title", "")),
            )
            for row in data.get("album_list") or []
        ]

    async def async_get_songs_in_album(
        self, album_id: int, group: SongGroup
    ) -> list[Song]:
        """List the songs within an album."""
        return self._songs_from(
            await self._command_json(
                "get_song_list_in_album",
                allow_empty=True,
                group=group.value,
                album_id=str(album_id),
            )
        )

    async def async_get_playlists(self, group: PlaylistGroup) -> list[Playlist]:
        """List the playlists in a library."""
        data = await self._command_json(
            "get_playlist_list", allow_empty=True, group=group.value
        )
        return [
            Playlist(
                playlist_id=int(row.get("playlist_id", 0)),
                title=str(row.get("playlist_title", "")),
            )
            for row in data.get("playlist_list") or []
        ]

    async def async_get_playlist_items(
        self, playlist_id: int, group: PlaylistGroup
    ) -> list[Song]:
        """List the items within a playlist."""
        return self._songs_from(
            await self._command_json(
                "get_item_list_in_playlist",
                allow_empty=True,
                group=group.value,
                playlist_id=str(playlist_id),
            )
        )

    async def async_refresh_library(self) -> None:
        """Reindex the PC Sharing Folder after files are added or removed over SMB.

        Files dropped onto the share are invisible to the library until the piano rescans;
        this is the "DB Reload" the web UI offers under Songs -> PC Sharing. Uses the
        internal ``setRefreshDB.php``; the open API has no equivalent.

        The rescan reassigns song ids, so resolve a song by title (``async_get_songs``)
        after refreshing rather than reusing an id from before.
        """
        await self._get(PATH_CTRL_REFRESH_DB)

    # ------------------------------------------------------------------
    # Radio
    # ------------------------------------------------------------------

    async def async_get_radio_channels(self) -> list[RadioChannel]:
        """List DisklavierRadio channels.

        Where the service is unavailable, the piano answers with an error envelope; the
        raised :class:`DisklavierResponseError` carries the envelope's ``error_info``.
        """
        data = await self._command_json("get_radio_channel_list")
        return [
            RadioChannel(
                channel_id=int(row.get("channel_id", 0)),
                title=str(row.get("channel_title", "")),
            )
            for row in data.get("channel_list") or []
        ]

    # ------------------------------------------------------------------
    # Song database and search
    # ------------------------------------------------------------------

    async def async_get_song_db(self, *, refresh: bool = False) -> SongDatabase:
        """Fetch the piano's own song database, from ``/ctrl/song.json``.

        This is the controller UI's backing store rather than part of the open API: one
        fetch describes every song in every library, including the media format the
        open API's listings omit. It is large -- most of a megabyte at two thousand
        songs -- so the parsed database is cached on the client. Pass ``refresh=True``
        to force a re-read, or let :meth:`async_lookup_song` refresh on a miss.
        """
        if refresh or self._song_db is None:
            data = await self._get_json(PATH_CTRL_SONG_DB, max_bytes=MAX_SONG_DB_BYTES)
            self._song_db = SongDatabase.from_json(data)
        return self._song_db

    async def async_lookup_song(self, prefix: str, song_id: int) -> LibrarySong | None:
        """Describe one song by the identity the sequencer reports for it.

        ``master.json`` names the loaded song as a library prefix and id (see
        :attr:`~aiodisklavier.models.MasterState.song_prefix`); this joins that pair
        against the song database. A miss refreshes the cached database once before
        giving up, because a fresh recording or a share re-index mints keys an older
        cache has never seen.
        """
        db = await self.async_get_song_db()
        song = db.lookup(prefix, song_id)
        if song is None:
            db = await self.async_get_song_db(refresh=True)
            song = db.lookup(prefix, song_id)
        return song

    async def async_search(self, query: str, *, limit: int = 20) -> list[SearchResult]:
        """Search songs, playlists and radio channels by title, best matches first.

        Matching happens in this library rather than on the piano: the open API's
        ``search_title`` can only *play* its single fuzzy pick, never return
        candidates. Songs come from the song database, so one fetch covers every
        library; playlists come from both playlist groups; radio channels are included
        where the service exists -- a region without DisklavierRadio answers with an
        error envelope, which here just means no radio results. Songs whose library
        prefix maps to no open-API group are left out, so every result can actually be
        played. Ties keep the piano's own ordering.
        """
        results: list[SearchResult] = []

        db = await self.async_get_song_db()
        for song in db.songs.values():
            if song.group is None:
                continue
            score = _match_score(query, song.title)
            if score:
                results.append(
                    SearchResult(
                        kind=SearchKind.SONG, title=song.title, score=score, song=song
                    )
                )

        for group in PlaylistGroup:
            for playlist in await self.async_get_playlists(group):
                score = _match_score(query, playlist.title)
                if score:
                    results.append(
                        SearchResult(
                            kind=SearchKind.PLAYLIST,
                            title=playlist.title,
                            score=score,
                            playlist=playlist,
                            playlist_group=group,
                        )
                    )

        try:
            channels = await self.async_get_radio_channels()
        except DisklavierResponseError:
            channels = []
        for channel in channels:
            score = _match_score(query, channel.title)
            if score:
                results.append(
                    SearchResult(
                        kind=SearchKind.RADIO,
                        title=channel.title,
                        score=score,
                        channel=channel,
                    )
                )

        results.sort(key=lambda result: result.score, reverse=True)
        return results[:limit]

    async def async_play_radio(self, channel_id: int) -> None:
        """Start a radio channel.

        Whether transport and playback commands are honoured while radio is playing has
        not been established on hardware -- treat their behaviour during radio as
        unknown. See the "Not established" notes in ``docs/enspire-api.md``.
        """
        await self._command("play_radio", channel_id=str(channel_id))

    async def async_stop_radio(self) -> None:
        """Stop radio playback."""
        await self._command("stop_radio")

    # ------------------------------------------------------------------
    # Snapshot and restore
    # ------------------------------------------------------------------

    async def async_snapshot_playback(self) -> PlaybackSnapshot:
        """Capture what is loaded and where, so it can be restored later.

        Reads the internal ``master.json``: reselecting a song needs the library prefix and
        song id, and ``/api/current_info`` reports neither.
        """
        return PlaybackSnapshot.from_master_json(
            await self._get_json(PATH_CTRL_MASTER_JSON)
        )

    async def async_restore_playback(self, snapshot: PlaybackSnapshot) -> None:
        """Put the piano back to a previously captured position.

        Restoring goes through the open API's ``load_song`` rather than the internal
        ``setSong.php``. That matters: ``setSong.php?prefix=&song_id=`` hardcodes
        ``control="play"`` in the firmware, so using it would start playback rather than
        merely reselect. ``load_song`` cues the song silently.

        A snapshot with no song loaded, or one whose library prefix is not recognised, is
        skipped rather than guessed at.
        """
        if not snapshot.has_song:
            return

        assert snapshot.song_prefix is not None
        assert snapshot.song_id is not None

        group = PREFIX_TO_SONG_GROUP.get(snapshot.song_prefix)
        if group is None:
            _LOGGER.debug(
                "Cannot restore playback: unrecognised library prefix %r",
                snapshot.song_prefix,
            )
            return

        # Stop first. 'load_song' only changes the sequencer's selection -- it does not
        # halt whatever is currently sounding. Restoring over a still-playing song
        # otherwise leaves the piano audibly playing one song while reporting another.
        await self.async_stop()

        await self.async_play_song(snapshot.song_id, group, load_only=True)
        if snapshot.position_ms:
            await self.async_seek(snapshot.position_ms)
        if snapshot.was_playing:
            await self.async_play()

    async def async_notify(
        self,
        *,
        song_id: int | None = None,
        group: SongGroup | None = None,
        search_title: str | None = None,
        volume: int | None = None,
        restore: bool = True,
        wait_timeout: float = NOTIFY_WAIT_TIMEOUT,
    ) -> None:
        """Play something once as a notification, then put the piano back as it was.

        Intended for doorbell- or alert-style automations. The notification plays as a
        one-shot, so the piano does not continue into the rest of the library afterwards.

        Specify the song either by ``song_id`` plus ``group``, or by ``search_title``.

        :param volume: Play at this volume, restoring the previous one afterwards.
        :param restore: Restore the previously loaded song and position when done.
        :param wait_timeout: Give up waiting for the notification to finish after this many
            seconds and restore anyway.

        Note that this takes over the sequencer. For a short sound that leaves playback
        untouched, use :meth:`async_play_test_chord` instead.
        """
        if (song_id is None or group is None) and not search_title:
            raise ValueError(
                "Provide either song_id together with group, or search_title"
            )

        snapshot = await self.async_snapshot_playback() if restore else None

        previous_volume: int | None = None
        if volume is not None:
            previous_volume = (await self.async_get_current_info()).volume

        took_over = False
        try:
            # Inside the try, so that a failure here still triggers the restore below.
            if volume is not None:
                await self.async_set_volume(volume)

            if search_title:
                await self.async_play_search(search_title, single=True)
            else:
                assert song_id is not None
                assert group is not None
                await self.async_play_song(song_id, group, single=True)
            took_over = True

            await self._async_wait_until_finished(wait_timeout)
        finally:
            # Best-effort, and never allowed to raise: a failure here would mask whatever
            # actually went wrong above. Restoring matters because otherwise a transient
            # error strands the piano on the notification track at notification volume.
            #
            # Silence the piano before touching the volume: on the give-up path the
            # notification can still be sounding, and restoring the previous -- usually
            # louder -- volume over it would blast its tail for a round-trip. Only once
            # the notification actually took the sequencer, though: if the play command
            # itself failed, whatever the user already had playing is still sounding,
            # and stopping it here would kill playback this method cannot always
            # restore.
            if took_over and (previous_volume is not None or snapshot is not None):
                await self._async_try_restore(self.async_stop(), "silence")
            if previous_volume is not None:
                await self._async_try_restore(
                    self.async_set_volume(previous_volume), "volume"
                )
            if snapshot is not None:
                await self._async_try_restore(
                    self.async_restore_playback(snapshot), "playback"
                )

    async def _async_try_restore(self, coro: Any, what: str) -> None:
        """Await a restore step, swallowing and logging any failure."""
        try:
            await coro
        except DisklavierError as err:
            _LOGGER.warning("Could not restore %s after notification: %s", what, err)

    async def _async_wait_until_finished(self, timeout: float) -> None:
        """Wait for the sequencer to stop playing, or until ``timeout`` elapses."""
        # The firmware takes a moment to flip playback_status to 'play'. Without this
        # settle the first poll would see the pre-play state and return immediately.
        await asyncio.sleep(NOTIFY_SETTLE)

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            info = await self.async_get_current_info()
            if not info.is_playing:
                return
            await asyncio.sleep(NOTIFY_POLL_INTERVAL)

        _LOGGER.debug(
            "Notification did not finish within %.1fs, restoring anyway", timeout
        )

    # ------------------------------------------------------------------
    # Miscellaneous
    # ------------------------------------------------------------------

    async def async_play_test_chord(self) -> None:
        """Audibly play a C major triad -- C4, E4, G4 at velocity 64 -- for one second.

        This addresses the MIDI patch daemon rather than the sequencer, so it leaves
        transport state untouched: it can be used while a song is loaded or paused without
        disturbing it. The chord is fixed; there are no parameters.

        The request blocks for the duration of the chord.
        """
        await self._get(PATH_CTRL_PUT_NOTE_ON)
