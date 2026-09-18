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
from aiodisklavier import client as client_module
from aiodisklavier.client import _match_score

from .conftest import (
    SONG_DB_PAYLOAD,
    FakePiano,
    dumps,
    ok_envelope,
    radio_channel_list,
)

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


async def test_song_db_parses_albums(piano: Disklavier) -> None:
    """Album rows come out keyed like songs, carrying their library and storage path.

    A PC Sharing Folder album is titled by its folder's path on the share, which is what
    lets a caller find a folder by name from this one fetch instead of the far slower
    album listing.
    """
    db = await piano.async_get_song_db()

    # Two rows carry an identity; the one with no ids is dropped.
    assert set(db.albums) == {"d9", "f22"}
    review = db.albums["f22"]
    assert review.album_id == 22
    assert review.title == "HousePianistApp/to-review"
    assert review.path == "FromToPC/HousePianistApp/to-review"
    assert review.group is SongGroup.PC_SHARING_FOLDER
    assert db.albums["d9"].group is SongGroup.BUILT_IN_SONGS


async def test_song_db_without_albums_parses(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A database with no album section still yields its songs, and no albums."""
    trimmed = json.loads(dumps(SONG_DB_PAYLOAD))
    del trimmed["album"]
    fake_piano.song_db_body = dumps(trimmed)

    db = await piano.async_get_song_db()
    assert db.albums == {}
    assert db.lookup("d", 1) is not None


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

    # Asked again -- a poller whose loaded song was deleted asks every few seconds --
    # the remembered miss answers without another download.
    assert await piano.async_lookup_song("r", 12345) is None
    assert await piano.async_lookup_song("r", 12345) is None
    assert (
        sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
        == fetches + 1
    )

    # Once something else re-reads the database, the missing key gets one more look.
    await piano.async_get_song_db(refresh=True)
    assert await piano.async_lookup_song("r", 12345) is None
    assert (
        sum(1 for r in fake_piano.requests if r.path == "/ctrl/song.json")
        == fetches + 3
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
    """Search covers songs, playlists and -- when asked -- radio, best matches first."""
    fake_piano.command_body = ok_envelope(
        playlist_list=[
            {"playlist_id": 3, "playlist_title": "Clair de lune covers"},
            # And one that does not match, which must simply not appear.
            {"playlist_id": 4, "playlist_title": "Morning Coffee"},
        ]
    )
    fake_piano.command_body_for = {
        "get_radio_channel_list": radio_channel_list(
            [
                {"channel_id": 5, "channel_title": "Clair de lune Radio"},
                {"channel_id": 6, "channel_title": "Hot Country Hits"},
            ]
        )
    }

    results = await piano.async_search("Clair de lune", include_radio=True)

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


async def test_search_does_not_touch_the_radio_unless_asked(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A plain search must never read the channel list, because doing so stops the music.

    Found on hardware: ``get_radio_channel_list`` stops the sequencer -- a playing song
    falls silent, a paused one is rewound. Search used to read it on every call, so
    every search from a media browser stopped whatever the piano was playing.
    """
    fake_piano.command_body = ok_envelope(playlist_list=[])

    results = await piano.async_search("Clair")

    assert results
    assert all(result.kind is not SearchKind.RADIO for result in results)
    assert "get_radio_channel_list" not in [r.command for r in fake_piano.requests]


async def test_searching_the_radio_reads_the_channel_list_once(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """With radio included, the interruption happens at most once per client."""
    fake_piano.command_body = ok_envelope(playlist_list=[])

    first = await piano.async_search("Chopin", include_radio=True)
    second = await piano.async_search("Chopin", include_radio=True)

    assert [r.kind for r in first] == [SearchKind.RADIO]
    assert first == second
    asked = [r for r in fake_piano.requests if r.command == "get_radio_channel_list"]
    assert len(asked) == 1


async def test_search_without_radio_still_answers(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A region without DisklavierRadio just contributes no radio results."""
    fake_piano.command_body = ok_envelope(playlist_list=[])
    fake_piano.command_body_for = {
        "get_radio_channel_list": dumps(
            {"status": "error", "error_info": "not available", "channel_list": []}
        )
    }

    results = await piano.async_search("Clair", include_radio=True)
    assert results
    assert all(result.kind is not SearchKind.RADIO for result in results)


async def test_search_does_not_hide_a_broken_channel_list(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the piano's considered "no" means "no radio results".

    A truncated reply is a fault. Search used to catch the broad response error around
    the radio read, which would have buried it behind an ordinary-looking result.
    """
    monkeypatch.setattr(client_module, "JSON_RETRY_DELAY", 0.0)
    fake_piano.command_body = ok_envelope(playlist_list=[])
    fake_piano.command_body_for = {"get_radio_channel_list": '{"status": "ok", "ch'}

    with pytest.raises(DisklavierResponseError):
        await piano.async_search("Clair", include_radio=True)


async def test_search_respects_the_limit(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The limit truncates after ranking."""
    fake_piano.command_body = ok_envelope(playlist_list=[], channel_list=[])

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
