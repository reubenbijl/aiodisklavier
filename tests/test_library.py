"""Tests for browsing the piano's libraries and radio."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    Disklavier,
    DisklavierEnvelopeError,
    Genre,
    GenreSelect,
    PlaylistGroup,
    SongGroup,
)
from aiodisklavier import client as client_module

from .conftest import CURRENT_INFO_PAYLOAD, FakePiano, dumps, ok_envelope

# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------


async def test_get_albums(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Albums are parsed from album_list."""
    fake_piano.command_body = ok_envelope(
        album_list=[{"album_id": 1, "album_title": "Pop"}],
    )
    albums = await piano.async_get_albums(SongGroup.BUILT_IN_SONGS)
    assert fake_piano.last.command == "get_album_list"
    assert fake_piano.last.query["group"] == "built_in_songs"
    assert albums[0].album_id == 1
    assert albums[0].title == "Pop"


async def test_get_songs_in_album(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Songs within an album are parsed."""
    fake_piano.command_body = ok_envelope(
        song_list=[{"song_id": 44, "song_title": "You're Welcome 2"}],
    )
    songs = await piano.async_get_songs_in_album(1, SongGroup.DOWNLOADED_SONGS)
    assert fake_piano.last.command == "get_song_list_in_album"
    assert fake_piano.last.query["album_id"] == "1"
    assert songs[0].song_id == 44


async def test_get_playlist_items(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Playlist contents are parsed."""
    fake_piano.command_body = ok_envelope(
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
    fake_piano.command_body = ok_envelope(something_else=[])
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
    assert fake_piano.last_command("play_radio").query["channel_id"] == "7"


async def test_play_radio_returns_once_the_channel_has_connected(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """``play_radio`` is acknowledged seconds before the channel is actually on.

    From hardware: the piano answers ``ok`` and then spends several seconds connecting,
    with an exclusive "Connecting..." dialog over its own interface. By default the call
    returns when ``master.json`` names the channel, so whatever the caller does next
    cannot land in the middle of that.
    """
    fake_piano.radio_obeys = False
    polls = 0

    def _connect_on_the_third_poll() -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            fake_piano.start_radio()

    fake_piano.on_master = _connect_on_the_third_poll

    await piano.async_play_radio(1)

    paths = [request.path for request in fake_piano.requests]
    assert paths.count("/ctrl/master.json") == 4
    assert paths[0].endswith("/play_radio")


async def test_play_radio_can_return_on_the_acknowledgement(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """``wait=False`` sends the command and nothing else."""
    await piano.async_play_radio(7, wait=False)
    assert [request.command for request in fake_piano.requests] == ["play_radio"]


async def test_play_radio_does_not_need_the_internal_state_to_work(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The wait is best-effort: it reads an unversioned endpoint, which may not be there.

    The channel has been started either way, so a piano that will not serve
    ``master.json`` costs the wait and nothing else.
    """
    fake_piano.ctrl_status = 404
    await piano.async_play_radio(7)
    paths = [request.path for request in fake_piano.requests]
    assert paths.count("/ctrl/master.json") == 1


async def test_play_radio_stops_waiting_eventually(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel that never reports connecting does not hold the caller for ever."""
    monkeypatch.setattr(client_module, "RADIO_CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "RADIO_CONNECT_POLL_INTERVAL", 0.01)
    fake_piano.radio_obeys = False
    await piano.async_play_radio(7)
    assert fake_piano.last.path == "/ctrl/master.json"


async def test_stop_radio(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Radio can be stopped."""
    await piano.async_stop_radio()
    assert fake_piano.last_command("stop_radio").path.endswith("/stop_radio")


async def test_stop_radio_returns_once_the_piano_will_listen(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """``stop_radio`` is answered about a second before the piano takes commands again.

    From hardware: the reply comes in under half a second, and for a second after it the
    piano is reloading the song it had before the radio and drops what it is sent -- a
    notification played straight after the reply was accepted with HTTP 200 and never
    sounded. The wait ends when the piano reports neither ``radio`` nor ``load``.
    """
    fake_piano.start_radio()
    fake_piano.radio_obeys = False
    statuses = iter(["radio", "load", "load", "pause"])

    def _advance() -> None:
        status = next(statuses, "pause")
        fake_piano.current_body = dumps(
            {**CURRENT_INFO_PAYLOAD, "playback_status": status}
        )

    _advance()
    fake_piano.on_current_info = _advance

    await piano.async_stop_radio()

    commands = [request.command for request in fake_piano.requests]
    assert commands == ["stop_radio"] + ["current_info"] * 4


async def test_stop_radio_can_return_on_the_reply(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """``wait=False`` sends the command and nothing else."""
    await piano.async_stop_radio(wait=False)
    assert [request.command for request in fake_piano.requests] == ["stop_radio"]


async def test_stop_radio_stops_waiting_eventually(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A piano that never reports leaving radio does not hold the caller for ever."""
    monkeypatch.setattr(client_module, "RADIO_EXIT_TIMEOUT", 0.05)
    monkeypatch.setattr(client_module, "RADIO_EXIT_POLL_INTERVAL", 0.01)
    fake_piano.start_radio()
    fake_piano.radio_obeys = False
    await piano.async_stop_radio()
    assert fake_piano.last.command == "current_info"


async def test_play_radio_reports_a_channel_the_account_lacks(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The piano's refusal to play a channel is raised, not swallowed.

    From hardware: ``play_radio`` for a channel outside the subscription answers HTTP
    200 with ``{"status":"error","error_info":"no subscription"}``. The radio commands
    are the only ones that answer with an envelope at all, and this one used to be sent
    without reading it -- so the call "succeeded" while the piano played nothing.
    """
    fake_piano.command_body_for = {
        "play_radio": '{"status":"error","error_info":"no subscription"}'
    }
    with pytest.raises(DisklavierEnvelopeError) as excinfo:
        await piano.async_play_radio(31)
    assert excinfo.value.command == "play_radio"
    assert excinfo.value.error_info == "no subscription"


# ----------------------------------------------------------------------
# Maintenance
# ----------------------------------------------------------------------


async def test_refresh_library(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Reindexing uses the internal endpoint; the open API has no equivalent."""
    await piano.async_refresh_library()
    assert fake_piano.last.path == "/ctrl/setRefreshDB.php"
