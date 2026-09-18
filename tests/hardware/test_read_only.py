"""Read-only checks against a real piano. Silent; nothing here changes its state.

A failure here is rarely a bug in the test. It is usually news: this piano said something
the library has no name for, which is exactly what is worth reporting.
"""

from __future__ import annotations

import aiohttp
import pytest

from aiodisklavier import (
    API_VERSION,
    PREFIX_TO_SONG_GROUP,
    SHARE_PC_SHARING,
    Disklavier,
    DisklavierCommandError,
    DisklavierShare,
    PlaylistGroup,
    SongGroup,
    _probe,
)


async def test_identity(piano: Disklavier) -> None:
    """The piano identifies itself, and speaks the API version this library targets."""
    info = await piano.async_get_static_info()
    assert info.disklavier_id
    assert info.version
    assert info.model
    assert info.api_version == API_VERSION


async def test_live_state_is_fully_recognised(piano: Disklavier) -> None:
    """Every status parses to a member. ``None`` means a value this library cannot name."""
    info = await piano.async_get_current_info()
    assert info.power_status is not None
    assert info.playback_status is not None
    # A model without a silent system may have nothing to say here; an *unknown* value
    # would have been logged as a warning, which pytest turns into a failure.
    assert info.volume is not None
    assert 0 <= info.volume <= 100


async def test_extended_state_parses(piano: Disklavier) -> None:
    """The internal state file is there and reads as expected."""
    master = await piano.async_get_master_state()
    assert master.repeat is not None
    assert master.library_updated is not None
    snapshot = await piano.async_snapshot_playback()
    assert snapshot.position_ms >= 0


async def test_song_database_is_fully_recognised(piano: Disklavier) -> None:
    """Every song's library and format is one this library knows."""
    database = await piano.async_get_song_db()
    assert database.songs
    assert database.update is not None
    unmapped = {song.prefix for song in database.songs.values() if song.group is None}
    assert not unmapped, f"library prefixes with no group: {sorted(unmapped)}"
    unformatted = [key for key, song in database.songs.items() if song.format is None]
    assert not unformatted, f"{len(unformatted)} songs in a format not recognised"


async def test_the_loaded_song_joins_to_the_database(piano: Disklavier) -> None:
    """``master.json`` names the loaded song the way the database keys it."""
    master = await piano.async_get_master_state()
    if master.song_prefix is None or master.song_id is None:
        pytest.skip("no song is loaded")
    song = await piano.async_lookup_song(master.song_prefix, master.song_id)
    assert song is not None
    assert song.prefix in PREFIX_TO_SONG_GROUP


@pytest.mark.parametrize("group", list(SongGroup))
async def test_song_libraries_list(piano: Disklavier, group: SongGroup) -> None:
    """Every library lists, an empty one as an empty list rather than an error."""
    songs = await piano.async_get_songs(group)
    albums = await piano.async_get_albums(group)
    assert all(isinstance(song.song_id, int) for song in songs)
    assert all(isinstance(album.album_id, int) for album in albums)


@pytest.mark.parametrize("group", list(PlaylistGroup))
async def test_playlists_list(piano: Disklavier, group: PlaylistGroup) -> None:
    """Both playlist groups list."""
    for playlist in await piano.async_get_playlists(group):
        assert isinstance(playlist.playlist_id, int)


async def test_my_songs_albums_carry_the_s_prefix(piano: Disklavier) -> None:
    """The ``s`` -> My Songs mapping, checked the way it was established.

    My Songs was empty on the reference piano, so ``s`` was never seen on a loaded song.
    It was established by joining the open API's album list for the group against the
    database's album keys -- and that join can be re-checked on any piano.
    """
    database = await piano.async_get_song_db()
    listed = {
        album.album_id for album in await piano.async_get_albums(SongGroup.MY_SONGS)
    }
    keyed = {a.album_id for a in database.albums.values() if a.prefix == "s"}
    assert listed == keyed


async def test_album_ids_agree_between_the_listing_and_the_database(
    piano: Disklavier,
) -> None:
    """The two sources name the same albums by the same ids. Titles are another matter."""
    database = await piano.async_get_song_db()
    listed = {
        album.album_id
        for album in await piano.async_get_albums(SongGroup.PC_SHARING_FOLDER)
    }
    keyed = {a.album_id for a in database.albums.values() if a.prefix == "f"}
    assert listed == keyed


async def test_search_answers_without_touching_the_radio(piano: Disklavier) -> None:
    """A plain search reads the database and the playlists, and nothing else."""
    before = await piano.async_get_current_info()
    results = await piano.async_search("a", limit=5)
    assert results
    after = await piano.async_get_current_info()
    # The position would have been rewound had the channel list been read.
    assert after.playback_status is before.playback_status
    if not before.is_playing:
        assert after.position_ms == before.position_ms


async def test_an_unknown_command_is_rejected_with_400(piano: Disklavier) -> None:
    """The open API's error contract: a bad request is an HTTP 400."""
    with pytest.raises(DisklavierCommandError):
        await piano._command("no_such_command")


async def test_the_share_answers(share: DisklavierShare) -> None:
    """The PC Sharing Folder is there, and lists."""
    assert SHARE_PC_SHARING in await share.async_list_shares()
    await share.async_list()
    assert await share.async_exists("aiodisklavier-no-such-path") is False


async def test_the_probe_reads_everything(host: str) -> None:
    """``python -m aiodisklavier`` finds nothing it cannot read or name on this piano."""
    async with aiohttp.ClientSession() as session:
        report = await _probe.probe(host, session, timeout=30)
    assert "could not read" not in report
    assert "NOT RECOGNISED" not in report
