"""Tests for the song database, format semantics, and search."""

from __future__ import annotations

import json

import pytest

from aiodisklavier import (
    Disklavier,
    DisklavierConnectionError,
    DisklavierResponseError,
    PlaylistGroup,
    SearchKind,
    SongFormat,
    SongGroup,
)
from aiodisklavier.client import _match_score

from .conftest import SONG_DB_PAYLOAD, FakePiano, dumps


def _ok(**payload: object) -> str:
    """Build the firmware's success envelope."""
    return dumps({"status": "ok", "error_info": "", **payload})


# ----------------------------------------------------------------------
# Database fetch and parsing
# ----------------------------------------------------------------------


async def test_song_db_is_parsed_and_cached(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """One fetch parses every usable row, and a second call reuses it."""
    db = await piano.async_get_song_db()

    assert db.update == 40157193
    # Five rows carry an identity; the one with no ids is dropped.
    assert len(db.songs) == 5
    angel = db.lookup("d", 1)
    assert angel is not None
    assert angel.title == "Angel"
    assert angel.format is SongFormat.SMF_MP3
    assert angel.group is SongGroup.BUILT_IN_SONGS
    assert angel.length_ms == 350760
    assert angel.performer == "Sarah McLachlan"
    assert angel.composer is None

    fetches = sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
    await piano.async_get_song_db()
    assert sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json") == fetches

    await piano.async_get_song_db(refresh=True)
    assert (
        sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
        == fetches + 1
    )


async def test_lookup_miss_refreshes_once(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A song the cache has never seen triggers one re-read, then resolves.

    A fresh recording or a share re-index mints new keys; the stale cache must not
    hide them, and an id that genuinely does not exist must not refetch forever.
    """
    await piano.async_get_song_db()

    grown = json.loads(dumps(SONG_DB_PAYLOAD))
    grown["song"]["r99"] = {
        "pfix": "r",
        "song_id": "99",
        "song_title": "New Recording",
        "format": "SMFSOLO",
    }
    fake_piano.song_db_body = dumps(grown)

    song = await piano.async_lookup_song("r", 99)
    assert song is not None
    assert song.title == "New Recording"
    assert song.group is SongGroup.MY_RECORDINGS

    fetches = sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
    assert await piano.async_lookup_song("r", 12345) is None
    assert (
        sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
        == fetches + 1
    )


async def test_song_db_larger_than_the_general_ceiling_is_accepted(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The database read uses its own ceiling, above MAX_RESPONSE_BYTES.

    The reference unit's database is already most of a megabyte; padding pushes this
    body past the general limit that every other endpoint keeps.
    """
    padded = json.loads(dumps(SONG_DB_PAYLOAD))
    padded["padding"] = "x" * (1024 * 1024 + 1024)
    fake_piano.song_db_body = dumps(padded)

    db = await piano.async_get_song_db()
    assert db.lookup("d", 1) is not None


# ----------------------------------------------------------------------
# Format semantics
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("format_", "has_audio"),
    [
        (SongFormat.SMF, False),
        (SongFormat.SMF_SOLO, False),
        (SongFormat.SMF_XG, True),
        (SongFormat.SMF_WAV, True),
        (SongFormat.SMF_MP3, True),
        (SongFormat.WAV, True),
    ],
)
def test_format_audio_rule(format_: SongFormat, has_audio: bool) -> None:
    """Audio-pair and XG formats use the speaker path; solo MIDI does not."""
    assert format_.has_audio is has_audio


async def test_unknown_format_degrades_to_none(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A format this library has never seen parses as None, not a crash."""
    mutated = json.loads(dumps(SONG_DB_PAYLOAD))
    mutated["song"]["d1"]["format"] = "SMF,OGG"
    fake_piano.song_db_body = dumps(mutated)

    song = await piano.async_lookup_song("d", 1)
    assert song is not None
    assert song.format is None
    assert song.has_audio is None


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------


async def test_search_ranks_and_spans_kinds(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Search covers songs, playlists and radio, best matches first."""
    fake_piano.command_body = _ok(
        playlist_list=[
            {"playlist_id": 3, "playlist_title": "Clair de lune covers"},
            # And one that does not match, which must simply not appear.
            {"playlist_id": 4, "playlist_title": "Morning Coffee"},
        ],
        channel_list=[
            {"channel_id": 5, "channel_title": "Clair de lune Radio"},
            {"channel_id": 6, "channel_title": "Hot Country Hits"},
        ],
    )

    results = await piano.async_search("Clair de lune")

    assert results[0].kind is SearchKind.SONG
    assert results[0].title == "Clair de lune"
    assert results[0].song is not None
    assert results[0].song.song_id == 24
    assert results[0].song.group is SongGroup.DOWNLOADED_SONGS

    kinds = {result.kind for result in results}
    assert SearchKind.PLAYLIST in kinds
    playlist_hit = next(r for r in results if r.kind is SearchKind.PLAYLIST)
    assert playlist_hit.playlist is not None
    assert playlist_hit.playlist.playlist_id == 3
    assert playlist_hit.playlist_group in tuple(PlaylistGroup)
    assert all(
        result.playlist is None or result.playlist.playlist_id != 4
        for result in results
    )

    assert SearchKind.RADIO in kinds
    radio_hit = next(r for r in results if r.kind is SearchKind.RADIO)
    assert radio_hit.channel is not None
    assert radio_hit.channel.channel_id == 5

    # The unmapped-prefix copy of the title must not appear: it cannot be played.
    assert all(result.song is None or result.song.prefix != "q" for result in results)


async def test_search_without_radio_still_answers(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A region without DisklavierRadio just contributes no radio results."""
    fake_piano.command_body = _ok(playlist_list=[])
    fake_piano.command_body_for = {
        "get_radio_channel_list": dumps(
            {"status": "error", "error_info": "not available", "channel_list": []}
        )
    }

    results = await piano.async_search("Clair")
    assert results
    assert all(result.kind is not SearchKind.RADIO for result in results)


async def test_search_respects_the_limit(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The limit truncates after ranking."""
    fake_piano.command_body = _ok(playlist_list=[], channel_list=[])

    results = await piano.async_search("Clair de lune", limit=1)
    assert len(results) == 1
    assert results[0].title == "Clair de lune"


def test_match_score_ordering() -> None:
    """Exact beats prefix beats substring beats fuzzy, and noise scores zero."""
    exact = _match_score("clair de lune", "Clair de lune")
    prefix = _match_score("Clair", "Clair de lune")
    substring = _match_score("lune", "Clair de lune")
    fuzzy = _match_score("Clare de loon", "Clair de lune")
    assert exact == 1.0
    assert exact > prefix > substring > fuzzy > 0.0
    assert _match_score("xyzzy", "Clair de lune") == 0.0
    assert _match_score("", "Clair de lune") == 0.0


# ----------------------------------------------------------------------
# Master state carries the loaded song's identity
# ----------------------------------------------------------------------


async def test_master_state_names_the_loaded_song(piano: Disklavier) -> None:
    """The sequencer's prefix and id survive into MasterState for database joins."""
    master = await piano.async_get_master_state()
    assert master.song_prefix == "y"
    assert master.song_id == 24


async def test_looking_up_the_loaded_song(piano: Disklavier) -> None:
    """MasterState identity joined against the database yields the loaded song."""
    master = await piano.async_get_master_state()
    assert master.song_prefix is not None
    assert master.song_id is not None

    song = await piano.async_lookup_song(master.song_prefix, master.song_id)
    assert song is not None
    assert song.title == "Clair de lune"
    assert song.format is SongFormat.SMF_XG
    assert song.has_audio is True


async def test_search_transport_failure_propagates(
    offline_piano: Disklavier,
) -> None:
    """An unreachable piano surfaces as a connection error, not a silent empty."""
    with pytest.raises(DisklavierConnectionError):
        await offline_piano.async_get_song_db()


async def test_song_db_rejects_a_runaway_body(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Even the raised ceiling is still a ceiling."""
    fake_piano.song_db_body = "x" * (8 * 1024 * 1024 + 4096)
    with pytest.raises(DisklavierResponseError):
        await piano.async_get_song_db()
