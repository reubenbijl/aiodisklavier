# aiodisklavier

Async Python client for the **Yamaha Disklavier ENSPIRE** local HTTP API.

Talks to the piano directly over your own network. Verified against firmware **5.24.00** on a Disklavier ENSPIRE PRO grand.

## Install

```bash
pip install aiodisklavier
```

## Use

```python
import aiohttp
from aiodisklavier import Disklavier, SongGroup

async with aiohttp.ClientSession() as session:
    piano = Disklavier("192.168.1.50", session)

    info = await piano.async_get_current_info()
    print(info.song_title, info.playback_status, info.position_seconds)

    # Fuzzy title search runs on the piano itself.
    await piano.async_play_search("Clair de lune")

    # Play one song and stop, rather than continuing through the library.
    await piano.async_play_song(24, SongGroup.DOWNLOADED_SONGS, single=True)
```

Finding the piano is a plain SSDP `M-SEARCH` for `urn:schemas-upnp-org:device:Disklavier:1`; the library exposes that device type as `UPNP_DEVICE_TYPE`.

## What it covers

| Area | Methods |
|---|---|
| State | `async_get_static_info`, `async_get_current_info`, `async_get_master_state` |
| Transport | `async_play`, `async_pause`, `async_stop`, `async_play_pause`, `async_next_song`, `async_previous_song`, `async_restart_song`, `async_seek` |
| Volume | `async_set_volume`, `async_volume_up`, `async_volume_down` |
| Power | `async_turn_on`, `async_turn_off`, `async_set_power` |
| Voicing | `async_set_quiet_mode`, `async_set_repeat` |
| Playback | `async_play_song`, `async_play_search`, `async_play_genre`, `async_play_album`, `async_play_playlist`, `async_play_playlist_item` |
| Browsing | `async_get_songs`, `async_get_albums`, `async_get_songs_in_album`, `async_get_playlists`, `async_get_playlist_items` |
| Radio | `async_get_radio_channels`, `async_play_radio`, `async_stop_radio` |
| Notifications | `async_notify`, `async_snapshot_playback`, `async_restore_playback`, `async_play_test_chord` |
| Library | `async_refresh_library` |

## Firmware behaviours worth knowing

These are properties of the piano, not of this library, and each is easy to get wrong. The
full reasoning, with provenance for every claim, is in
[docs/enspire-api.md](docs/enspire-api.md).

- **There is no stop state.** `stop` leaves `playback_status` reading `pause` at position
  zero. Use `CurrentInfo.is_stopped` rather than looking for a stop constant.
- **Waking takes about twelve seconds**, during which `power_status` reads `wakeup` and the
  piano ignores commands. The HTTP API answers normally while asleep, so reachability tells
  you nothing about power state.
- **Empty libraries are an error, not an empty list** — HTTP 200 carrying
  `{"status": "error", "error_info": "no song"}`.
- **State reads can come back truncated** while a song is playing, because the daemon
  rewrites those files in place. Reads retry automatically. Payloads may also carry a
  trailing `\n\0`, which is stripped rather than retried.
- **State lags a command.** Reading `current_info` straight after a `load_song` or reselect
  returns the *previous* song. Allow a short settle before trusting a post-command read.
- **While radio is playing, transport commands are silently ignored** and still return 200.
- **`async_play_test_chord` makes a sound** — a C major triad for one second. It goes to the
  MIDI daemon rather than the sequencer, so it will not disturb a loaded song.

## Two APIs, one preferred

The piano exposes a versioned open API at `/api/1.0/<command>` and an internal, unversioned
set of endpoints under `/ctrl/` that its own web UI drives. This library uses the open API
wherever possible and drops to `/ctrl/` only for what the open API cannot do: seeking, repeat
and shuffle, the extended state block, reindexing, and the test chord. Both funnel XML into
the same Unix socket inside the piano.

The open API takes some finding: nothing the piano normally serves links to it, and neither
the phone app nor the piano's own web UI calls it. The one client-side trail is
`/ctrl/api_test.html`, a test harness Yamaha ships on the device — that is where the
`/api/1.0/` form is visible. `/api/api.php?_com=<command>` is the same surface by another
name, verified equivalent down to the error codes.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest
```

Tests run against a real `aiohttp` test server that imitates the piano, so no hardware is
needed and the suite does not depend on any mocking library's grip on aiohttp internals.
Several tests encode behaviour found only on real hardware — those are commented as such,
because they look arbitrary otherwise.

## Licence

MIT
