"""Tests for the Disklavier client, against a fake piano served over real HTTP."""

from __future__ import annotations

import pytest

from aiodisklavier import (
    Disklavier,
    DisklavierCommandError,
    DisklavierConnectionError,
    DisklavierResponseError,
    PlaylistGroup,
    PowerStatus,
    QuietMode,
    RepeatMode,
    SongGroup,
)
from aiodisklavier import client as client_module
from aiodisklavier.const import PATH_API_BASE

from .conftest import CURRENT_INFO_PAYLOAD, FakePiano, dumps

# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------


async def test_get_static_info(piano: Disklavier) -> None:
    """Static info is fetched and parsed."""
    info = await piano.async_get_static_info()
    assert info.disklavier_id == "DKV000000000000"
    assert info.model == "PRO"


async def test_get_current_info(piano: Disklavier) -> None:
    """Current info is fetched and parsed."""
    info = await piano.async_get_current_info()
    assert info.volume == 100
    assert info.position_ms == 516000


async def test_get_master_state_uses_internal_endpoint(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Extended state comes from the internal master.json."""
    master = await piano.async_get_master_state()
    assert master.repeat is RepeatMode.OFF
    assert fake_piano.last.path == "/ctrl/master.json"


# ----------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------


async def test_http_400_raises_command_error(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """The firmware signals every bad argument as a plain 400."""
    fake_piano.command_status = 400
    fake_piano.command_body = "400 Bad Request"
    with pytest.raises(DisklavierCommandError):
        await piano.async_play()


async def test_error_envelope_raises_response_error(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """An empty library is an error envelope inside HTTP 200."""
    fake_piano.command_body = dumps(
        {"status": "error", "error_info": "no song", "song_list": []}
    )
    with pytest.raises(DisklavierResponseError, match="no song"):
        await piano.async_get_songs(SongGroup.MY_SONGS)


async def test_invalid_json_raises_response_error(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """A persistently non-JSON body is reported clearly rather than crashing."""
    fake_piano.current_body = "<html>nope</html>"
    with pytest.raises(DisklavierResponseError):
        await piano.async_get_current_info()


async def test_truncated_json_is_retried(
    piano: Disklavier, fake_piano: FakePiano, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A part-written state file must be re-read, not surfaced as a fault.

    The daemon rewrites these files in place, so a poll can catch one mid-write. Seen on
    real hardware: /api/current_info came back cut off at 203 bytes.
    """
    monkeypatch.setattr(client_module, "JSON_RETRY_DELAY", 0.0)

    truncated = dumps(CURRENT_INFO_PAYLOAD)[:203]
    fake_piano.current_body = truncated

    def _heal() -> None:
        fake_piano.current_body = dumps(CURRENT_INFO_PAYLOAD)

    fake_piano.on_current_info = _heal

    info = await piano.async_get_current_info()
    assert info.volume == 100
    # One truncated read, then a good one.
    assert len(fake_piano.requests) == 2


async def test_null_terminated_json_is_accepted(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    r"""A complete payload with the daemon's trailing ``\n\0`` must parse as-is.

    Seen on real hardware: /api/current_info arrives as ``{...}\n\x00``. The NUL is a
    stable terminator, not a truncated read, so it must be stripped -- not retried.
    """
    fake_piano.current_body = dumps(CURRENT_INFO_PAYLOAD) + "\n\x00"
    info = await piano.async_get_current_info()
    assert info.volume == 100
    # Parsed on the first read; no retry.
    assert len(fake_piano.requests) == 1


async def test_connection_failure_raises_connection_error(
    offline_piano: Disklavier,
) -> None:
    """Transport failures surface as a connection error."""
    with pytest.raises(DisklavierConnectionError):
        await offline_piano.async_get_current_info()


async def test_timeout_raises_connection_error(
    server, session, fake_piano: FakePiano
) -> None:
    """A piano that accepts the connection but never answers is a connection error."""
    fake_piano.delay = 1.0
    slow = Disklavier(server.host, session, port=server.port, timeout=0.05)
    with pytest.raises(DisklavierConnectionError, match="Timeout"):
        await slow.async_get_current_info()


async def test_non_object_json_raises_response_error(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Valid JSON that is not an object is still unusable."""
    fake_piano.current_body = dumps([1, 2, 3])
    with pytest.raises(DisklavierResponseError, match="unexpected JSON"):
        await piano.async_get_current_info()


async def test_host_property(piano: Disklavier, server) -> None:
    """The client exposes the host it was built with."""
    assert piano.host == server.host


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("async_play", "play"),
        ("async_pause", "pause"),
        ("async_stop", "stop"),
        ("async_play_pause", "play_pause"),
        ("async_next_song", "next_song"),
        ("async_previous_song", "prev_song"),
        ("async_restart_song", "back_song"),
        ("async_volume_up", "volume_up_main"),
        ("async_volume_down", "volume_down_main"),
    ],
)
async def test_transport_commands(
    piano: Disklavier, fake_piano: FakePiano, method: str, expected: str
) -> None:
    """Each transport helper hits the right versioned command path."""
    await getattr(piano, method)()
    assert fake_piano.last.path.startswith(f"{PATH_API_BASE}/")
    assert fake_piano.last.command == expected


async def test_set_volume_sends_value(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Volume is sent as _v."""
    await piano.async_set_volume(42)
    assert fake_piano.last.command == "set_volume_main"
    assert fake_piano.last.query["_v"] == "42"


@pytest.mark.parametrize("volume", [-1, 101, 999])
async def test_set_volume_rejects_out_of_range(piano: Disklavier, volume: int) -> None:
    """Out-of-range volumes fail locally rather than as an opaque 400."""
    with pytest.raises(ValueError, match="volume must be between"):
        await piano.async_set_volume(volume)


async def test_power_uses_valueless_flag(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Power is a valueless flag, not a key=value pair.

    The firmware tests ``isset($_GET["sleep"])``, so the flag must be present with an empty
    value. Sending ``sleep=on`` would happen to work, but ``&sleep`` is what the app does.
    """
    await piano.async_turn_off()
    assert fake_piano.last.command == "set_power_status"
    assert "sleep" in fake_piano.last.query
    assert fake_piano.last.query["sleep"] == ""
    assert "sleep=" in fake_piano.last.query_string


async def test_turn_on_uses_on_flag(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Waking uses the 'on' flag."""
    await piano.async_turn_on()
    assert fake_piano.last.command == "set_power_status"
    assert "on" in fake_piano.last.query


async def test_wakeup_cannot_be_requested(piano: Disklavier) -> None:
    """WAKEUP is reported by the piano, never commanded."""
    with pytest.raises(ValueError, match="cannot be requested"):
        await piano.async_set_power(PowerStatus.WAKEUP)


async def test_quiet_mode_uses_valueless_flag(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Quiet mode is also a valueless flag."""
    await piano.async_set_quiet_mode(QuietMode.QUIET)
    assert fake_piano.last.command == "set_quiet_status"
    assert fake_piano.last.query["quiet"] == ""


async def test_seek_uses_internal_endpoint(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Seeking is only possible through setSeq.php."""
    await piano.async_seek(120000)
    assert fake_piano.last.path == "/ctrl/setSeq.php"
    assert fake_piano.last.query["time"] == "120000"


async def test_seek_rejects_negative(piano: Disklavier) -> None:
    """A negative seek is a programming error, caught locally."""
    with pytest.raises(ValueError, match="must not be negative"):
        await piano.async_seek(-1)


async def test_set_repeat_uses_internal_endpoint(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Repeat is only possible through setSong.php."""
    await piano.async_set_repeat(RepeatMode.MEDIA_SHUFFLE)
    assert fake_piano.last.path == "/ctrl/setSong.php"
    assert fake_piano.last.query["repeat"] == "media_shuffle"


# ----------------------------------------------------------------------
# Library browsing
# ----------------------------------------------------------------------


async def test_song_list_uses_song_list_key(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Song groups return song_list."""
    fake_piano.command_body = dumps(
        {
            "status": "ok",
            "error_info": "",
            "song_list": [{"song_id": 1, "song_title": "Angel"}],
        }
    )
    songs = await piano.async_get_songs(SongGroup.BUILT_IN_SONGS)
    assert songs == [songs[0]]
    assert songs[0].song_id == 1
    assert songs[0].title == "Angel"


async def test_song_list_accepts_item_list_key(
    piano: Disklavier, fake_piano: FakePiano
) -> None:
    """Playlist-ish groups return item_list instead, keyed by item_id."""
    fake_piano.command_body = dumps(
        {
            "status": "ok",
            "error_info": "",
            "item_list": [{"item_id": 7, "song_title": "Down in History"}],
        }
    )
    songs = await piano.async_get_songs(SongGroup.BUILT_IN_PLAYLIST)
    assert songs[0].song_id == 7
    assert songs[0].title == "Down in History"


async def test_get_playlists(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Playlists are parsed."""
    fake_piano.command_body = dumps(
        {
            "status": "ok",
            "error_info": "",
            "playlist_list": [{"playlist_id": 1, "playlist_title": "RR Christmas"}],
        }
    )
    playlists = await piano.async_get_playlists(PlaylistGroup.PLAYLISTS)
    assert playlists[0].playlist_id == 1
    assert playlists[0].title == "RR Christmas"


async def test_get_radio_channels(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Radio channels are parsed."""
    fake_piano.command_body = dumps(
        {
            "status": "ok",
            "channel_list": [{"channel_id": 1, "channel_title": "Sampler"}],
        }
    )
    channels = await piano.async_get_radio_channels()
    assert channels[0].channel_id == 1
    assert channels[0].title == "Sampler"


async def test_play_song_load_only(piano: Disklavier, fake_piano: FakePiano) -> None:
    """load_only cues a song without starting it."""
    await piano.async_play_song(24, SongGroup.DOWNLOADED_SONGS, load_only=True)
    assert fake_piano.last.command == "load_song"
    assert fake_piano.last.query["id"] == "24"
    assert fake_piano.last.query["group"] == "downloaded_songs"


async def test_play_song_plays(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Without load_only the song starts."""
    await piano.async_play_song(24, SongGroup.DOWNLOADED_SONGS)
    assert fake_piano.last.command == "play_song"


async def test_play_search(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Fuzzy search is passed straight through."""
    await piano.async_play_search("Clair")
    assert fake_piano.last.command == "play_song"
    assert fake_piano.last.query["search_title"] == "Clair"


async def test_play_search_rejects_empty_title(piano: Disklavier) -> None:
    """An empty search would match unpredictably, so it is refused."""
    with pytest.raises(ValueError, match="must not be empty"):
        await piano.async_play_search("")


@pytest.mark.parametrize(
    ("load_only", "single", "expected"),
    [
        (False, False, "play_song"),
        (False, True, "play_single_song"),
        (True, False, "load_song"),
        # load_only wins: there is nothing to play, so 'single' is meaningless.
        (True, True, "load_song"),
    ],
)
async def test_song_command_selection(
    piano: Disklavier,
    fake_piano: FakePiano,
    load_only: bool,
    single: bool,
    expected: str,
) -> None:
    """Cue, play, and play-one-and-stop map to distinct firmware commands."""
    await piano.async_play_song(
        1, SongGroup.BUILT_IN_SONGS, load_only=load_only, single=single
    )
    assert fake_piano.last.command == expected


async def test_play_single_search(piano: Disklavier, fake_piano: FakePiano) -> None:
    """Search supports the one-shot variant too."""
    await piano.async_play_search("Clair", single=True)
    assert fake_piano.last.command == "play_single_song"


async def test_play_test_chord(piano: Disklavier, fake_piano: FakePiano) -> None:
    """The test chord goes to the MIDI daemon endpoint, not the sequencer."""
    await piano.async_play_test_chord()
    assert fake_piano.last.path == "/ctrl/putNoteOn.php"
