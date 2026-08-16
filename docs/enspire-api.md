# Yamaha Disklavier ENSPIRE — local API

Reference for building a local-control integration, compiled entirely from what the piano
itself serves on your own network: an HTTP API for control and state (§1–§7), and an SMB
share for getting content onto the instrument (§8). Verified against a unit running
**5.24.00** (`PRO`, grand, region `World`).

Every claim is marked with where it came from:

- **[live]** — observed by calling the piano and reading what came back. The strongest
  evidence here: the device's actual behaviour, not a description of it.
- **[api-test]** — `/ctrl/api_test.html`, an API test page the piano serves. Yamaha's own
  harness, and effectively the API's documentation.
- **[app]** — the piano's web app, served at `/ctrl/main.php` with its JavaScript under
  `/js/`. What the piano's own UI does.
- **[inferred]** — deduced from observed behaviour, not directly confirmed.

Everything below is reproducible with `curl` and a browser against a stock piano. Where a fact
could not be established that way it is either omitted or listed in §8 as unknown, rather than
guessed at.

---

## 1. There are two APIs

**Use the first one.**

| | `/api/` — *open API* | `/ctrl/` — *internal* |
|---|---|---|
| Contract | Versioned (`api_version` 1.0, rev 1) | Unversioned |
| Shape | `/api/1.0/<command>?<params>`, JSON with a `status`/`error_info` envelope | One endpoint per subsystem, no envelope |
| Errors | Proper HTTP 400 | HTTP 200 regardless; errors echoed into the body |
| Coverage | Transport, volume, power, quiet, library, radio | Everything, including settings the app exposes |
| Intended for | External consumers | The piano's own web UI |

Build against `/api/` and drop to `/ctrl/` only for what `/api/` doesn't cover (§5).

Neither requires authentication on a piano with no passcode set. **[live]**

### Finding the open API

Worth stating plainly, because it is easy to miss: **almost nothing the piano serves points at
the open API.** The web UI drives `/ctrl/` directly — mirror all 41 of its JavaScript files and
you will extract 47 endpoints, none of them under `/api/`. The ENSPIRE Controller phone app is
only a launcher: it calls `getDescription.php`, `putNoteOn.php` and `setSystem.php`, then opens
the piano's web UI in a webview. **[app]**

The one trail is **`/ctrl/api_test.html`**, an 81 KB test harness the piano serves at that
path. Nothing links to it, so you have to know the URL — but it is present on a stock piano, it
exercises the open API end to end, and it is where the versioned `/api/1.0/` form appears:
**[api-test]**

```js
url: '../api/1.0/' + elem + parm,
$.getJSON("../api/1.0/static_info", ...)
$.getJSON("../api/1.0/current_info", ...)
```

It also carries a table of 78 worked example calls covering nearly every command, which is
where much of §4 comes from. Open it in a browser against your own piano and it will drive the
API live.

---

## 2. Discovery

**SSDP M-SEARCH** on `239.255.255.250:1900`: **[live]**

```
ST: urn:schemas-upnp-org:device:Disklavier:1
```

The piano answers with a `LOCATION` on port 49152. Fetching it — or
`GET /ctrl/getDescription.php` on port 80, which returns the same document — gives: **[live]**

```xml
<deviceType>urn:schemas-upnp-org:device:Disklavier:1</deviceType>
<friendlyName>DKV000000000000</friendlyName>
<manufacturer>YAMAHA CORPORATION</manufacturer>
<modelName>Disklavier ENSPIRE 5.24.00</modelName>
<presentationURL>http://192.168.1.50/ctrl/</presentationURL>
```

Prefer `/api/1.0/static_info` (§3) once you have an address — it is cleaner and gives a stable
id.

**Host facts [live]:** lighttpd/1.4.35, PHP/5.4.27. Open ports: **80** (app + API), **139** and
**445** (Samba — the PC Sharing Folder share), **49152** (UPnP description). `/` 302-redirects
to `/ctrl/main.php`. MAC OUI `24:76:25` (Yamaha).

SMB is a second route for getting content onto the piano: drop a `.mid` into the *PC Sharing
Folder* share, call `GET /ctrl/setRefreshDB.php` to reindex, then play it by title. **[live]**

> **Not MusicCast.** `/YamahaExtendedControl/v1/...` returns 404, as do the Mark IV's
> `db_access/*.php` and `YamahaRemoteControl/ctrl.php`. Libraries written for MusicCast do not
> apply to this device. **[live]**

---

## 3. Open API — state

### `GET /api/1.0/static_info` **[live]**

Device identity. Does not change while the piano runs, so fetch it once.

```json
{ "api_version":"1.0", "api_revision":"1", "disklavier_id":"DKV000000000000",
  "enspire_region":"World", "enspire_version":"5.24.00", "enspire_model":"PRO",
  "piano_type":"grand" }
```

`disklavier_id` is the natural unique id for an integration.

### `GET /api/1.0/current_info` **[live]**

Everything a media player needs, in one poll.

```json
{ "power_status":"on", "quiet_status":"acoustic", "playback_status":"pause",
  "playback_position":"516000", "volume_main":"100",
  "song_title":"Beethoven - Symphony No. 7, Movement 1.",
  "song_artist":"Ludwig van Beethoven",
  "song_folder":"Liszt's Beethoven's Symphony No. 7",
  "song_length":"851900" }
```

Positions and lengths are **milliseconds, as strings**. Every scalar is a string, numbers
included.

---

## 4. Open API — commands

`GET /api/1.0/<command>?<params>`. An unknown command, an unknown `group`, or an out-of-range
argument all return **HTTP 400** with the body `400 Bad Request`. **[live]**

### Two spellings of the same API **[live]**

| Form | Example |
|---|---|
| Version-namespaced (**preferred**) | `/api/1.0/set_volume_main?_v=50` |
| CGI form | `/api/api.php?_com=set_volume_main&_v=50` |

Checked for parity directly: identical bodies, identical 400s for an unknown command, an
unknown `group` and an out-of-range `_v`, and identical valueless-flag handling. Prefer the
versioned path — it carries the version `static_info` reports in `api_version`, and it is what
the piano's own test page calls. `/api/static_info` and `/api/1.0/static_info` both work,
likewise `current_info`.

### Transport **[api-test]**

`play` · `pause` · `stop` · `play_pause` · `next_song` · `prev_song` · `back_song` (restart
current song) · `record` · `test_rec` · `smartkey_unpause`

All of these except the recording pair are confirmed **[live]**.

### Volume and power

| Command | Args | |
|---|---|---|
| `set_volume_main` | `_v=0..100`; out of range → 400 | **[live]** |
| `volume_up_main` / `volume_down_main` | no args; steps by **10** | **[live]** |
| `set_power_status` | `on` or `sleep` as a **valueless flag** | **[live]** |
| `set_quiet_status` | `acoustic` or `quiet`, same style | **[live]** |
| `dkvtv_offline_mode` | `enter` or `exit` | **[api-test]** |

> **The flag style is real.** `/api/1.0/set_power_status?sleep` — a bare flag, no value.
> `?sleep=` works too, since presence is all that is checked, but omitting the flag entirely
> returns 400. Confirmed both ways. **[live]**

### Library groups **[live]**

Wherever `group=` appears. The right-hand column is the one-letter prefix the same library
reports in `master.json`, which matters for restoring a selection (§6).

| `group` | Prefix | |
|---|---|---|
| `built_in_songs` (also accepted as `built_in_song`) | `d` | factory library, 500 songs here |
| `built_in_playlist` | `l` | |
| `my_songs` | *unverified* | empty on this unit, so its prefix was never observed |
| `my_recordings` | `r` | |
| `pc_sharing_folder` | `f` | the SMB share |
| `downloaded_songs` | `y` | |

Playlist-specific groups: `demo_playlist` and `playlists`. **[api-test]**

### Choosing what to play **[api-test]**

| Command | Args |
|---|---|
| `load_song` `play_song` `play_single_song` | `id=&group=` · or `search_title=` · or `group=built_in_songs&folder=<genre>&select=top\|random` |
| `load_album` `play_album` | `album_id=&group=` or `search_title=` |
| `load_playlist` `play_playlist` | `playlist_id=&group=` or `search_title=` |
| `load_playlist_item` `play_playlist_item` `play_single_playlist_item` | `item_id=&group=` |

Three verbs, three behaviours, all confirmed **[live]**:

- **`load_*`** cues without starting. `playback_status` stays `pause`.
- **`play_*`** starts, and continues into the rest of the library afterwards.
- **`play_single_*`** plays one item and stops — the right primitive for a notification.

`search_title` fuzzy-matches on the piano, so partial titles work: `search_title=Clair`
resolves to "Clair de lune". The test page includes non-Latin examples (`search_title=ムーン`),
so it is not ASCII-only. **[live]** / **[api-test]**

**Genres** for `folder=`, with the song-id span each covers. Derived by listing the built-in
albums and reading the ids in each — the genre folders and the built-in albums are the same
thing: **[live]**

| Genre | Album | Song ids | | Genre | Album | Song ids |
|---|---|---|---|---|---|---|
| `pop` | Pop | 1–64 | | `country` | Country | 192–204 |
| `rock` | Rock | 65–74 | | `holidays` | Holidays | 205–231 |
| `jazz` | Jazz | 75–131 | | `soundtrack` | Soundtrack | 232–249 |
| `rnb_soul` | R&B / Soul | 132–141 | | `piano50` | 50 Greats for the Piano | 250–299 |
| `classical` | Classical | 142–191 | | `lesson` | Lesson | 300–489 |
| | | | | `smartkey` | SmartKey | 490–500 |

### Listing the libraries **[live]**

`get_song_list&group=` · `get_album_list&group=` · `get_song_list_in_album&group=&album_id=` ·
`get_playlist_list&group=` · `get_item_list&group=` · `get_item_list_in_playlist&group=&playlist_id=`

```json
{"status":"ok","error_info":"","song_list":[{"song_id":28,"song_title":"20220905_181027"}, …]}
{"status":"ok","error_info":"","playlist_list":[{"playlist_id":1,"playlist_title":"RR Christmas"}, …]}
```

Two traps, both confirmed:

- **The list key changes with the group.** `get_song_list&group=built_in_playlist` returns
  **`item_list`** keyed by `item_id`, where a song group returns `song_list` keyed by
  `song_id`. Accept either.
- **An empty library is an error, not an empty list** — HTTP 200 carrying
  `{"status":"error","error_info":"no song","song_list":[]}`. Always check `status`.

### Radio **[api-test]**

`get_radio_channel_list` · `play_radio&channel_id=<int>` · `stop_radio`. The listing works and
returned 51 channels on this unit. **[live]**

---

## 5. Internal API — what `/api/` doesn't cover

`GET /ctrl/<endpoint>.php?<params>`. These are what the piano's own web UI calls, so the
parameter names below are what it actually sends. **[app]** They return HTTP 200 regardless of
whether they worked — an invalid value is simply ignored — so the status code tells you
nothing. **[live]**

### `setSeq.php` — transport, including seek

`status=` `play` `pause` `stop` `ffs` (fast forward) `frs` (fast rewind) `back_song` `record`
`rec_wait` `audio_rec_wait` `smf_audio_rec_wait` **[app]**

**`time=<ms>` is the only way to seek** — the open API cannot seek at all. Confirmed exact:
seeking to 516000 reads back 516000. **[live]**

Also `ab_repeat=` for A–B repeat. **[app]**

### `setSong.php` — selection and repeat mode

`prefix=` `song_id=` `control=next` **[app]**

`repeat=` takes `off` `one` `media_all` `media_shuffle` `album_all` `album_shuffle`
`playlist_all` `playlist_shuffle`. All eight were sent and read back from `master.json`.
**[live]** Repeat and shuffle are one combined setting here, with no open API equivalent.

> ⚠️ Using `setSong.php?prefix=&song_id=` to reselect a song **starts playback** — it does not
> merely cue. To reselect quietly, use the open API's `load_song`. **[live]**

### `setVol.php` — per-channel mixer

One channel per call: `main` `headphone` `tg` `audio` `voice` `omni_in` `omni_out`
`digital_out` `metronome` `rec_level`, plus `mute=on|off`. **[app]**

### Others the web app drives **[app]**

| Endpoint | Params it sends |
|---|---|
| `setPiano.php` | `quiet=` `voice=` `reverb_type=` `reverb_depth=` |
| `setPlayFunc.php` | `left_hand=` `right_hand=` (part muting for practice) |
| `setRcs.php` | `status=` `on` `sleep` `rlesson` `rlive` `to_maint_user` |
| `setSystem.php` | `lang=` `autooff_time=` `hp_vol_ctrl_status=` |
| `setAudioIO.php` | `omni_in_type=` `omni_in_delay=` `omni_out_type=` `digital_out=` `piano_delay=` `sync_out_level=` |
| `setMidiIO.php` | `midi_in_port=` `midi_out_port=` `midi_out_type=` `piano_rcv_ch=` `kbd_out_ch=` |
| `setIdcfunc.php` | `function=` `play_radio` `stop_radio` `radio_channel_list` `all_purchased_list` `last_purchased_list` `add_downloadlist` `end_downloadlist` `save_notification` |
| `setOnDemand.php` | `status=on\|off` · `cmd=ondemandStop` |
| `setGeneralXML.php` | `element=` `set_metronome` `metronome_modal` `set_timerplay` `onoff_timerplay` `purchased_list_end_off` |
| `setSbc.php` | `led=` |
| `setFilefunc.php` | `function=move_to_keep` |
| `setReset.php` | `set_reset=` — ⚠️ a reset; do not fire speculatively |
| `setRemoteLesson.php` / `setRemotelive.php` | `cmd=exit` |
| `setPage.php` | `event=` (UI page tracking) |
| `setRefreshDB.php` | none — reindexes the song database. Returns 200; indexing takes a few seconds **[live]** |

`setGeneralXML.php` takes an `element=` naming the thing to change plus that element's own
parameters, rather than a fixed signature, which suggests a generic passthrough rather than a
typed endpoint. **[inferred]**

### Read-only helpers **[app]**

`getDescription.php` · `getSysInfo.php` · `getManufacture.php` · `getKey.php` ·
`getDate.php?cmd=` · `getContentURL.php` · `getUSBFileExists.php?check=` ·
`getCoverArt.php?pfix=…` · `ondemand_CoverArt.php?songId=…` · `retMsg.php?key=`

### `putNoteOn.php` — makes a sound **[live]**

The Controller app uses this as a reachability check, but it is **not silent**: it audibly
plays a fixed chord. The request blocks for the duration, measured at 1.08 s.

It leaves transport state completely untouched — `current_info` is byte-identical before and
after — so unlike anything in §4 it can be used while a song is loaded or paused without
disturbing it. It takes no parameters.

---

## 6. Internal state files

`GET /ctrl/<name>.json`. The web app polls these; the intervals below are its own. **[app]**

| File | Interval | Verified | Contents |
|---|---|---|---|
| `master.json` | 500 ms | **[live]** | Main state blob — superset of `current_info` |
| `settings.json` | 500 ms | **[live]** | MIDI/audio I/O, network, timezone |
| `performance.json` | 500 ms | **[live]** | Per-channel volumes, play functions |
| `song.json` | on demand | **[live]** | Full song library (~302 KB here) |
| `idc.json` | on demand | **[live]** | Cloud/account state |
| `ondemand_status.json`, `ondemand_song.json`, `ondemand_playlist.json` | — | **[live]** | On-demand |
| `rlesson*.json`, `remotelive*.json` | 500–1000 ms | 404 when idle | Only exist while that mode runs |

`master.json` adds, over `current_info`: `seq` (`tempo`, `bar`, `sync`, `skip_space`, and
crucially **`song_pfix` + `song_id`**, the only place the current selection's library is
reported), `ab_repeat`, `repeat`, `vol.headphone`, `mute`, `re_rec`, a full `piano` block
(voice, reverb, metronome `met_*`, `key_motion`), `sbc` (headphone/USB/WLAN/LED/timer/
auto-off), `system` (`master_tune`, `login_passcode`, `demo`), `msgbox` (modals the piano
pushes at its client), `apictrl`, `radio`, `login`, `firmware`. **[live]**

`performance.json` carries the mixer (`tg` `audio` `voice` `omni_in` `omni_out` `digital_out`
`metronome` `rec_level` `rec_level_peak`) and `playfunc` (`trans` = transpose, `left_hand`,
`right_hand`, `pedal`). **[live]**

---

## 7. Behaviours you only find on hardware

None of these are inferable from the interface. All were found by driving a real piano.

1. **There is no stop state.** `stop` sets `playback_status` to **`pause`** with
   `playback_position` `0`. Distinguish stopped from paused by position, or track it yourself.
2. **`power_status` has a third value: `wakeup`.** Waking goes `sleep → wakeup → on` and takes
   about **12 seconds**, during which commands are ignored. The HTTP API answers normally while
   asleep, so reachability says nothing about power state.
3. **State reads can come back truncated.** The state files are rewritten in place, so a poll
   can catch one part-written — seen as `current_info` cut off mid-field. Only ever observed
   while a song was playing; 160 reads on an idle piano were all clean. Retry.
4. **Payloads may carry a trailing `\n\0`.** A stable terminator, not a truncated read. Strip
   it; do not retry on it.
5. **State lags a command.** Reading straight after a `load_song` or reselect returns the
   *previous* song. Allow a short settle before trusting a post-command read.
6. **`master.json` truncates one repeat mode to 15 characters.** Setting `playlist_shuffle`
   reads back as `playlist_shuffl`, stably across repeated reads. The command is accepted
   correctly; only the state file is wrong. No other mode is long enough to be affected.
7. **An unplayable file silently resets the selection.** Loading a macOS `._`-prefixed
   AppleDouble stub from the PC Sharing Folder returned 200 and left the piano selected on
   `d`/1 — the first built-in song — rather than reporting an error.

   To reproduce you need such a stub actually present and indexed: copy a file to the share
   from macOS, then `GET /ctrl/setRefreshDB.php`, and the `._` companion will appear in
   `get_song_list&group=pc_sharing_folder` as a song in its own right. Delete the stub
   (`dot_clean -m "/Volumes/PC Sharing Folder"`) and reindex, and the behaviour goes with it —
   a `load_song` on the now-absent id is accepted, leaves the selection untouched, and still
   returns 200. So a stale id is a silent no-op; only an id backed by an unplayable *file*
   causes the reset.
8. **`load_song` does not stop what is playing.** It changes the sequencer's selection only.
   Restoring over a still-playing song leaves the piano audibly playing one thing while
   reporting another; stop first.
9. **Position resolution is ~1000 ms** and advances in real time during playback.
10. **`volume_up_main` / `volume_down_main` step by 10.**

### Errors — all confirmed HTTP 400 on the open API

Invalid command · unknown `group` · `_v` out of range · non-numeric `_v` · no command at all ·
`set_quiet_status` with neither flag.

**The 400s cover malformed arguments, not missing content.** A `song_id` or `album_id` that
does not exist is accepted and returns 200, doing nothing. Never treat a 200 from a
`load_*`/`play_*` call as proof that the thing you asked for was found — resolve ids from a
live listing, or read the state back afterwards (allowing for the lag in §7.5).

The internal `/ctrl/` endpoints do **not** behave this way: `setSong.php?repeat=nonsense`
returns 200 and silently keeps the previous value.

---

## 8. SMB — the PC Sharing Folder

The second way onto the piano, and the only way to add content: write a `.mid` to the share,
`GET /ctrl/setRefreshDB.php` to reindex, then play it by title. Everything here is **[live]**
against the same unit.

### The server is SMB1-only, and that decides your library

Two version numbers matter here and they are easy to conflate. **Samba 3.0.37** is the server
*software* the piano runs — a 2010 release, reported in the `IPC$` share comment. **SMB1**,
also called NT1, is the *protocol dialect* that software speaks. Samba did not gain SMB2 at
all until 3.6, so this server has no dialect newer than SMB1 to offer, and an SMB2
`NEGOTIATE` gets no reply: the socket is closed.

```
smbprotocol → SMBConnectionClosed: SMB socket was closed
pysmb       → connected, shares listed, files transferred
```

macOS agrees, reporting `SMB_VERSION SMB_1` for its own mount of the share (`smbutil
statshares -a`) even though the client offers SMB1/2/3.

So a client library has to still speak NT1. **`smbprotocol` supports 2.0.2 upwards and cannot
talk to this piano at all**; `pysmb` can, and negotiates SMB2 where a server offers it, so
choosing it costs nothing if a later firmware moves on. This is why `aiodisklavier` depends
on pysmb.

### Shares and access

| Share | |
|---|---|
| `PC Sharing Folder` | read-write, the one that matters |
| `ENSPIRE Controller` | read-only, the controller's own files |
| `IPC$` | the usual pipe; its comment is where the Samba version shows up |

Stock firmware serves these to **guests**: a session was granted for `guest` with no password,
for a username that does not exist, and for an empty username alike, with full write access
each time. NTLMv2 and NTLMv1 both work. Treat the share as unauthenticated and the LAN as the
security boundary — the same trust model as the HTTP API.

### Behaviours worth knowing

- **NT status codes are conventional.** A missing leaf answers `0xC0000034`
  (`OBJECT_NAME_NOT_FOUND`), a missing parent `0xC000003A` (`OBJECT_PATH_NOT_FOUND`), creating
  a directory that exists `0xC0000035` (`OBJECT_NAME_COLLISION`), and removing a populated
  directory `0xC0000101` (`DIRECTORY_NOT_EMPTY`).
- **Timestamps are the time of the write, at one-second resolution**, and the piano's clock
  agrees with real time. So "the local file is newer than its copy" is a sound test for
  "changed since it was last sent" — which is what makes an incremental mirror possible
  without keeping a manifest on the share.
- **The indexer descends exactly two folder levels, and says nothing about the third.** This
  is the one that will cost you an afternoon. `<folder>/<subfolder>/song.mid` is indexed and
  the subfolder becomes an album; `<folder>/<sub>/<sub>/song.mid` is copied fine, lists fine
  over SMB, and never appears in the library — no error from the write, none from the
  reindex.

  Established by planting the same file at three depths and reindexing:

  | Path | Indexed |
  |---|---|
  | `ImpromptuApp/depthprobe/DEPTH2.mid` | yes — album `ImpromptuApp/depthprobe` |
  | `ImpromptuApp/depthprobe/lvl3/DEPTH3.mid` | no |
  | `ImpromptuApp/depthprobe/lvl3/lvl4/DEPTH4.mid` | no |

  A search for `DEPTH3` fuzzy-matched back to `DEPTH2`, confirming the deeper two were
  absent from the database rather than merely unlisted. A catalogue laid out as
  `<root>/maestro/<Composer>/*.mid` is one level too deep; flatten it to
  `<root>/<Composer>/*.mid`. With that done, 1297 files across 61 folders indexed as 61
  albums, and `get_song_list&group=pc_sharing_folder` returned all **1326** songs on the
  share in one response — that listing is not paginated or capped.
- **A folder holding songs is an album.** `get_album_list&group=pc_sharing_folder` reports
  each such folder with its path as the title, which is the only way to enumerate a large
  share by structure. Folders that contain nothing but other folders are not albums.
- **An audio file sharing a MIDI file's basename is that song's backing track, not a song.**
  Drop `song.mid` and `song.wav` in together and the firmware pairs them: the keys play the
  MIDI, the audio plays through the speakers. This is Yamaha's SMF+Audio, and it is what
  makes a transcription sound like the record rather than a piano reduction of it.

  The pairing is invisible in the listing, which is what makes it easy to miss. The share
  root here holds nine files — seven `.mid` and two `.mp3` — and indexes exactly **seven**
  songs. Neither MP3 appears on its own.

  Confirmed by adding one `.wav` beside an already-indexed `.mid` and reindexing:

  | | Songs in that folder | Reported `endtime` |
  |---|---|---|
  | `Patient.mid` alone | 21 | 187558 ms |
  | `Patient.mid` + `Patient.wav` | 21 | **190589 ms** |

  The song count did not move, so the WAV was not indexed separately — and the duration
  became the *audio* file's 190.6 s rather than the MIDI's 187.6 s, so it was not ignored
  either. **The audio drives the song length.** Both `.wav` (44.1 kHz, 16-bit stereo) and
  `.mp3` work.

  The trap: filter a mirror to `{".mid"}` and every transcription still copies, still
  indexes, and still plays — as a bare piano part with the band missing. Nothing reports it.
  `aiodisklavier`'s `PLAYABLE_SUFFIXES` includes audio for this reason, and
  `async_sync_directory` logs a warning if a MIDI is sent while its companion is filtered
  out.
- **Do not let macOS write to the share directly.** It leaves `._` AppleDouble companions
  beside every file, and the indexer treats those as songs — see §7.7 for what loading one
  does. Anything writing to this share should exclude them; `aiodisklavier` does by default.
- **Throughput is roughly 1 MB/s** over SMB1 on this LAN — 84.4 MB of MIDI in 87 s, about 15
  files a second, the per-file round trip dominating rather than the bytes. A single session
  carries one request at a time, so there is nothing to be gained by transferring in
  parallel on one connection. Re-scanning the same 1297 files to find nothing to do takes
  0.8 s, because it costs one directory walk rather than a stat per file.

---

## 9. Not established

Stated plainly rather than guessed at, because everything above is reproducible and these are
not:

- **The `my_songs` prefix.** That library was empty on this unit, so its one-letter prefix in
  `master.json` was never observed.
- **Whether radio blocks transport commands.** There is reason to think playback behaves
  differently while a radio channel is playing, but this was not exercised and should be
  treated as unknown.
- **Recording.** `record`, `rec_wait`, `audio_rec_wait` and `smf_audio_rec_wait` appear in the
  web app but were deliberately never fired.
- **Value ranges** for most `/ctrl/` parameters. Only `set_volume_main` (0–100) was probed at
  its bounds.
- **`rlesson*` / `remotelive*` schemas.** Those files 404 unless the mode is running.
- **Behaviour with a passcode set.** This piano had none. `setPasscode.php` and `setLogin.php`
  exist and are unexplored.
- **Whether any of this holds across models, regions or firmware.** One piano, one version.
  Reports from other units are welcome.

## Reproducing this

```bash
PIANO=192.168.1.50

curl -s "http://$PIANO/api/1.0/static_info"
curl -s "http://$PIANO/api/1.0/current_info"
curl -s "http://$PIANO/api/1.0/get_song_list?group=built_in_songs"

# Yamaha's own test page — drives the API live against your piano.
open "http://$PIANO/ctrl/api_test.html"
```
