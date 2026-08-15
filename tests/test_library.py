"""Tests for browsing the piano's libraries and radio."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    Disklavier,
    Genre,
    GenreSelect,
    PlaylistGroup,
    SongGroup,
)

from .conftest import FakePiano, dumps


def _ok(**payload: object) -> str:
    """Build the firmware's success envelope."""
    return dumps({"status": "ok", "error_info": "", **payload})


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------


async def test_get_albums(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Albums are parsed from album_list."""
    fake_piano.command_body = _ok(
        album_list=[{"album_id": 1, "album_title": "Pop"}],
    )
    albums = await piano.async_get_albums(SongGroup.BUILT_IN_SONGS)
    assert fake_piano.last.command == "get_album_list"
    assert fake_piano.last.query["group"] == "built_in_songs"
    assert albums[0].album_id == 1
    assert albums[0].title == "Pop"


async def test_get_songs_in_album(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Songs within an album are parsed."""
    fake_piano.command_body = _ok(
        song_list=[{"song_id": 44, "song_title": "You're Welcome 2"}],
    )
    songs = await piano.async_get_songs_in_album(1, SongGroup.DOWNLOADED_SONGS)
    assert fake_piano.last.command == "get_song_list_in_album"
    assert fake_piano.last.query["album_id"] == "1"
    assert songs[0].song_id == 44


async def test_get_playlist_items(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Playlist contents are parsed."""
    fake_piano.command_body = _ok(
        item_list=[{"item_id": 24, "song_title": "Silent Night"}],
    )
    items = await piano.async_get_playlist_items(1, PlaylistGroup.PLAYLISTS)
    assert fake_piano.last.command == "get_item_list_in_playlist"
    assert fake_piano.last.query["playlist_id"] == "1"
    assert items[0].title == "Silent Night"


async def test_list_without_a_recognised_key_is_empty(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A success envelope carrying neither list key yields no songs, not an error.

    The firmware switches between ``song_list`` and ``item_list`` by group; anything else
    is treated as an empty result rather than a crash.
    """
    fake_piano.command_body = _ok(something_else=[])
    assert await piano.async_get_songs(SongGroup.BUILT_IN_SONGS) == []


# ----------------------------------------------------------------------
# Playback selection
# ----------------------------------------------------------------------


async def test_play_album(piano: Disklavier, fake_piano: FakePiano) -> None:
    """An album can be played by id."""
    await piano.async_play_album(3, SongGroup.BUILT_IN_SONGS)
    assert fake_piano.last.command == "play_album"
    assert fake_piano.last.query["album_id"] == "3"


async def test_load_album(piano: Disklavier, fake_piano: FakePiano) -> None:
    """An album can be cued without playing."""
    await piano.async_play_album(3, SongGroup.BUILT_IN_SONGS, load_only=True)
    assert fake_piano.last.command == "load_album"


async def test_play_playlist(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A playlist can be played by id."""
    await piano.async_play_playlist(1, PlaylistGroup.PLAYLISTS)
    assert fake_piano.last.command == "play_playlist"
    assert fake_piano.last.query["playlist_id"] == "1"


async def test_load_playlist(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A playlist can be cued without playing."""
    await piano.async_play_playlist(1, PlaylistGroup.PLAYLISTS, load_only=True)
    assert fake_piano.last.command == "load_playlist"


async def test_play_playlist_item(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A single playlist entry can be played."""
    await piano.async_play_playlist_item(24, PlaylistGroup.PLAYLISTS)
    assert fake_piano.last.command == "play_playlist_item"
    assert fake_piano.last.query["item_id"] == "24"


async def test_load_playlist_item(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A single playlist entry can be cued without playing."""
    await piano.async_play_playlist_item(24, PlaylistGroup.PLAYLISTS, load_only=True)
    assert fake_piano.last.command == "load_playlist_item"


@pytest.mark.parametrize(
    ("select", "expected"),
    [(GenreSelect.TOP, "top"), (GenreSelect.RANDOM, "random")],
)
async def test_play_genre(
    piano: Disklavier, fake_piano: FakePiano, select: GenreSelect, expected: str
) -> None:
    """A genre folder resolves to a song-id span on the built-in library."""
    await piano.async_play_genre(Genre.JAZZ, select=select)
    assert fake_piano.last.command == "play_song"
    assert fake_piano.last.query["folder"] == "jazz"
    assert fake_piano.last.query["select"] == expected
    assert fake_piano.last.query["group"] == SongGroup.BUILT_IN_SONGS.value


async def test_load_genre(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A genre pick can be cued without playing."""
    await piano.async_play_genre(Genre.CLASSICAL, load_only=True)
    assert fake_piano.last.command == "load_song"


# ----------------------------------------------------------------------
# Radio
# ----------------------------------------------------------------------


async def test_play_radio(piano: Disklavier, fake_piano: FakePiano) -> None:
    """A radio channel is started by id."""
    await piano.async_play_radio(7)
    assert fake_piano.last.command == "play_radio"
    assert fake_piano.last.query["channel_id"] == "7"


async def test_stop_radio(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Radio can be stopped."""
    await piano.async_stop_radio()
    assert fake_piano.last.command == "stop_radio"


# ----------------------------------------------------------------------
# Maintenance
# ----------------------------------------------------------------------


async def test_refresh_library(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Reindexing uses the internal endpoint; the open API has no equivalent."""
    await piano.async_refresh_library()
    assert fake_piano.last.path == "/ctrl/setRefreshDB.php"
