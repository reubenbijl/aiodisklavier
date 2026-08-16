# aiodisklavier

Async Python client for the **Yamaha Disklavier ENSPIRE** local API — HTTP for control and
state, SMB for getting your own music onto the instrument.

Talks to the piano directly over your own network. Verified against firmware **5.24.00** on a Disklavier ENSPIRE PRO grand.

> Not affiliated with, endorsed by, or sponsored by Yamaha Corporation. Yamaha, Disklavier
> and ENSPIRE are trademarks of Yamaha Corporation, used here only to identify the hardware
> this library talks to.

## Install

```bash
pip install aiodisklavier
```

## Use

```python
import asyncio

import aiohttp

from aiodisklavier import Disklavier, SongGroup


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        piano = Disklavier("192.168.1.50", session)

        info = await piano.async_get_current_info()
        print(info.song_title, info.playback_status, info.position_seconds)

        # Fuzzy title search runs on the piano itself.
        await piano.async_play_search("Clair de lune")

        # Play one song and stop, rather than continuing through the library.
        await piano.async_play_song(24, SongGroup.DOWNLOADED_SONGS, single=True)


asyncio.run(main())
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
  `{"status": "error", "error_info": "no song"}`. The browse methods translate that
  envelope back into the empty list it denotes, so callers just see `[]`.
- **State reads can come back truncated** while a song is playing, because the daemon
  rewrites those files in place. Reads retry automatically. Payloads may also carry a
  trailing `\n\0`, which is stripped rather than retried.
- **State lags a command.** Reading `current_info` straight after a `load_song` or reselect
  returns the *previous* song. Allow a short settle before trusting a post-command read.
- **Radio's interaction with transport commands is not established.** There is reason to
  think playback behaves differently while a radio channel is playing, but it has not been
  exercised on hardware — treat transport during radio as unknown.
- **`async_play_test_chord` makes a sound** — a C major triad for one second. It goes to the
  MIDI daemon rather than the sequencer, so it will not disturb a loaded song.

## Getting music onto the piano

The piano also exports an SMB share — the *PC Sharing Folder* — and that is the only route
for adding your own MIDI. `DisklavierShare` covers it in the same async, typed idiom, so a
consumer of this library can browse, upload and mirror without shelling out to `mount`.

```python
from aiodisklavier import Disklavier, DisklavierShare, PLAYABLE_SUFFIXES


async with DisklavierShare("192.168.1.50") as share:
    for entry in await share.async_list():
        print(entry.path, entry.size, entry.modified)

    await share.async_upload("doorbell.mid", "ImpromptuApp/doorbell.mid")

    # Mirror a local library. Only what changed is sent, so repeat runs are cheap and an
    # interrupted one resumes.
    result = await share.async_sync_directory(
        "~/Music/disklavier", "ImpromptuApp", suffixes=PLAYABLE_SUFFIXES
    )

if result.changed:
    await piano.async_refresh_library()  # nothing is playable until the piano reindexes
```

| Area | Methods |
|---|---|
| Browsing | `async_list`, `async_walk`, `async_stat`, `async_exists`, `async_list_shares` |
| Writing | `async_upload`, `async_upload_bytes`, `async_makedirs`, `async_rename` |
| Reading | `async_download`, `async_download_bytes` |
| Removing | `async_delete`, `async_remove_directory`, `async_delete_tree` |
| Mirroring | `async_sync_directory` |

Things worth knowing about the share, all covered in
[docs/enspire-api.md §8](docs/enspire-api.md):

- **It speaks SMB1 and nothing newer.** Samba 3.0.37 is the server *software*; SMB1/NT1 is
  the newest protocol dialect it can offer, because Samba did not gain SMB2 until 3.6. An
  SMB2 negotiate gets the socket closed. This is why the dependency is `pysmb` and not
  `smbprotocol`, which starts at SMB 2.0.2 and cannot connect to the piano at all.
- **The piano indexes exactly two folder levels.** `<folder>/<subfolder>/song.mid` is the
  deepest path it will ever list, and each subfolder holding songs becomes an album.
  Anything below that copies without complaint and then simply is not in the library — no
  error from the write, none from the reindex. `async_sync_directory` logs a warning when it
  uploads past the limit, but it will not restructure a tree for you:

  ```text
  ImpromptuApp/Frédéric Chopin/Ballade No. 1 in G Minor, Op. 23.mid   indexed
  ImpromptuApp/maestro/Frédéric Chopin/Ballade No. 1 in G Minor.mid   invisible
  ```
- **Audio next to a MIDI file is that song's backing track.** `song.mid` + `song.wav` (or
  `.mp3`) with matching basenames is one SMF+Audio song, not two: the keys play the MIDI,
  the speakers play the audio, and the audio's length becomes the song's duration. Filter a
  sync to `{".mid"}` and every transcription still copies, indexes and plays — as a bare
  piano part with the band missing, silently. `PLAYABLE_SUFFIXES` includes audio for exactly
  this reason, and `async_sync_directory` warns if it sends a MIDI whose companion was
  filtered out.
- **Never copy to the share from macOS directly.** Finder leaves a `._` AppleDouble stub
  beside every file, the piano indexes those as songs in their own right, and loading one
  silently resets the piano to the first built-in song. `async_sync_directory` excludes them
  — along with `.DS_Store` and friends — by default.
- **Nothing you upload is playable until you reindex** with `async_refresh_library`, and the
  reindex reassigns song ids, so resolve songs by title afterwards rather than reusing an id.
- **The share is served to guests** on stock firmware — no password, full write access. Same
  trust model as the HTTP API: the LAN is the security boundary.
- **Transfers are serial.** One SMB session carries one request, so `DisklavierShare`
  serialises operations behind a lock. Use separate instances if you want parallelism.

## Two APIs, one preferred

The piano exposes a versioned open API at `/api/1.0/<command>` and an internal, unversioned
set of endpoints under `/ctrl/` that its own web UI drives. This library uses the open API
wherever possible and drops to `/ctrl/` only for what the open API cannot do: seeking, repeat
and shuffle, the extended state block, reindexing, and the test chord.

The open API takes some finding: nothing the piano normally serves links to it, and neither
the phone app nor the piano's own web UI calls it. The one client-side trail is
`/ctrl/api_test.html`, a test harness Yamaha ships on the device — that is where the
`/api/1.0/` form is visible. `/api/api.php?_com=<command>` is the same surface by another
name, verified equivalent down to the error codes.

## Security

The piano's API is plaintext HTTP with no authentication (unless a passcode is set on the
piano), and SSDP discovery answers are unauthenticated multicast — any host on the LAN can
observe or impersonate the piano. The client hardens itself against a hostile device:
response bodies are read against a size ceiling, redirects are refused, and device-supplied
strings are treated as data. The transport itself still has no confidentiality or
integrity, so keep this traffic on a trusted network and do not expose the piano or this
client across an untrusted one.

The SMB share is the same picture, and a little worse: stock firmware serves it to guests
with full write access, and SMB1 has no meaningful integrity protection. `DisklavierShare`
refuses paths containing `..` or control characters before they reach the wire, so a
device- or config-supplied path cannot be steered outside the share, and it never follows
a name the server invents. Treat write access to the share as equivalent to LAN access.

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

MIT — see [LICENSE](LICENSE).

This project documents and interoperates with a local network interface exposed by hardware
the owner already has on their own network. It contains no Yamaha source code, firmware or
musical content, and circumvents no access control: the piano's local API is unauthenticated
as shipped. The protocol was derived by observing traffic, not by decompiling anything.

Yamaha, Disklavier and ENSPIRE are trademarks of Yamaha Corporation. This project is not
affiliated with, endorsed by, or sponsored by Yamaha Corporation.
