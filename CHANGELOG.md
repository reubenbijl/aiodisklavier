# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The last incompatible changes before 1.0, made now because they only get more expensive, and
the fixes from a morning spent finding out what the piano really does with a radio, a
notification and a restore. Several of these change behaviour a caller may be relying on —
**read "Changed" before upgrading.** Everything marked *found on hardware* is written up,
with its evidence, in `docs/enspire-api.md`.

### Changed

- **Every model is keyword-only.** `Album(1, "Pop")` is now a `TypeError`; write
  `Album(album_id=1, title="Pop")`. Twice already a field had to be appended with a default
  purely so positional callers kept working, and after 1.0 every new firmware field would
  have carried that constraint for good. New fields now arrive with defaults, so code that
  builds a model — a test fixture, usually — keeps working across releases. Applies to
  everything in `aiodisklavier.__all__` that is a dataclass, the share's `ShareEntry` and
  `Sync*` results included.
- **A status this library does not recognise is `None`, not a guess.**
  `CurrentInfo.power_status`, `quiet_status` and `playback_status` are now `... | None`, where
  an unknown value used to become `ON`, `ACOUSTIC` and `PAUSE`. That went wrong on the
  reference piano itself — see the two new values below, both of which were being read as
  their fallbacks — and the next unknown value will come from a piano nobody here has met. An
  unrecognised value is logged once, as a warning that names it and asks for a report; an
  absent one is silent. **Callers that read `.value` off a status, or `match` on one without
  a default arm, need a `None` case.** `mypy` will find them.
- **`CurrentInfo.is_playing` is true during radio**, since the piano is making music, and
  `is_stopped` is true only for `PAUSE` at position zero — no longer for a piano that is
  loading, or in a state this library cannot name.
- **An HTTP 4xx other than 400 raises `DisklavierResponseError`**, carrying the status as
  `http_status`. It used to fall through `raise_for_status` into `DisklavierConnectionError`,
  which is the error a poller treats as "unreachable, try again" — so a piano that simply
  does not serve an endpoint, the likeliest way another model or firmware will differ, would
  have been retried for ever and reported as offline. 5xx is still a connection error, now
  deliberately and with the status in the message.
- **`async_search` leaves radio out unless asked**: pass `include_radio=True`. See the first
  entry under "Fixed" for why the default had to change.
- **`async_get_radio_channels` caches.** The first call reads the list and later ones are
  served from the client; pass `refresh=True` to read it again. A real read also now returns
  only once the sequencer has settled from the reset it causes.
- **`async_play_radio` and `async_stop_radio` wait**, by default, until the channel has
  connected and until the piano will take a command again — a few seconds and about one
  second. Pass `wait=False` for the old fire-and-forget. Both now read the piano's reply, so
  a declined request raises instead of passing silently.
- **`async_restore_playback` takes two to four seconds longer**, because it now does what it
  says. See "Fixed".
- `PREFIX_TO_SONG_GROUP` is a read-only mapping.

### Added

- **`PlaybackStatus.LOAD` and `PlaybackStatus.RADIO`, and `QuietMode.HEADPHONE`** — three
  values the piano reports that this library had no names for. `RADIO` stands for as long as
  a DisklavierRadio channel is active; `LOAD` for a second or two while the sequencer loads a
  song; `HEADPHONE` for as long as headphones are plugged in. `HEADPHONE` cannot be requested
  — the piano answers 400 — so `async_set_quiet_mode` refuses it locally, as `async_set_power`
  refuses `WAKEUP`. *Found on hardware.*
- **`CurrentInfo.is_radio`**, and **`MasterState.radio_channel` / `radio_title`**: the
  programme, which is reported nowhere else — `current_info` goes blank during radio.
- **`DisklavierEnvelopeError`**, a subclass of `DisklavierResponseError`, for a command the
  piano understood and declined — `"no subscription"` from `play_radio`, say. It separates the
  piano's considered "no" from garbage on the wire, which the parent class used to mean as
  well. Code that catches `DisklavierResponseError` and reads `command` / `error_info` off it
  is unaffected.
- **`python -m aiodisklavier <host>`** describes a piano for a bug report: which fields it
  sends, which values its status fields take, and anything this library does not recognise.
  Read-only and silent — it never asks for the radio channel list — and it prints shapes
  rather than contents, because the piano keeps the account's email address and a passcode in
  the same file as its playback state. Made to be pasted straight into the new **piano
  report** issue template.
- **A hardware test suite**, `tests/hardware/`, that runs the library against a real piano:
  `DISKLAVIER_HOST=<ip> pytest -m hardware`. Read-only by default, with opt-in levels for
  tests that change settings and for tests that drive the sequencer. Skipped entirely without
  the variable, so CI and the coverage gate are unaffected. See `CONTRIBUTING.md`.
- **The public surface is written down.** `aiodisklavier.__all__` is the API; submodule paths
  and underscore names are not. Newly exported to make that true for names the docstrings
  already pointed at: `SMBBackend` — the `connection_factory` seam had no importable type —
  and `API_VERSION`, `DEFAULT_TIMEOUT`, `SMB_TIMEOUT`, `NOTIFY_WAIT_TIMEOUT`,
  `MAX_RESPONSE_BYTES`, `MAX_SONG_DB_BYTES` and `PREFIX_TO_SONG_GROUP`. The README gains a
  **Stability** section saying what 1.0 will and will not promise.
- `PlaybackSnapshot.radio_active` / `radio_channel`, and `DisklavierResponseError.http_status`.
- `SECURITY.md`, issue templates, a CI job that tests against the lowest dependency versions
  the package claims to support, and a macOS leg.

### Fixed

- **Searching no longer stops the music.** `get_radio_channel_list` looks like a read and is
  not: it stops the sequencer, so a song that was playing falls silent and one that was paused
  is rewound to the start. `async_search` read it on every call — so every search from a media
  browser stopped whatever the piano was playing. *Found on hardware.*
- **A notification sent while the radio is on now sounds.** The piano answers every `load_*`
  and `play_*` with HTTP 200 during radio and ignores them, so `async_notify` was accepted,
  played nothing, and "restored" a snapshot that paired the previously loaded song's id with
  the *radio's* clock. It now ends the radio first — waiting out the second after
  `stop_radio`'s reply during which the piano is still deaf — and puts the channel back on
  afterwards. A snapshot taken during radio records the channel and no longer trusts the
  sequencer block. *Found on hardware.*
- **`async_restore_playback` restores the position, and resumes a song that was playing.** On
  the reference piano it was doing neither. `load_song` answers at once and the sequencer
  then loads for about two seconds, dropping what it is sent meanwhile; the restore sent its
  seek and its `play` straight after, so the piano came back on the right song, at the start,
  stopped — a plain MIDI file and an MP3-backed song alike. The tests checked that the
  requests went out. The restore now
  waits for the load, reads the position back, and sends a dropped seek or `play` once more —
  once, because this daemon does not reward persistence. *Found on hardware*, and the reason
  the hardware suite exists.
- **`async_notify` no longer ends its wait at the first `load`.** On the way out of radio the
  piano reports `load` for longer than the settle, which read as "finished" and had the
  restore stop the notification before its first note.
- **`async_play_radio` reports a channel the account cannot play.** The piano answers
  `{"status":"error","error_info":"no subscription"}` inside HTTP 200, and the reply was not
  being read.
- **`async_search` no longer hides a broken channel list.** It caught the broad response
  error around the radio read, so a truncated reply looked like a region without radio.
- The library paces its own compound operations: a second between `stop` and `load_song`, no
  second `stop` for a piano that is already stopped, nothing sent to a song that is loading.
  A precaution against the sequencer wedging, whose cause is not established — see
  `docs/enspire-api.md` §7.14.
- `pytest-asyncio` is floored at 0.24, the first release to know the config option the suite
  sets; 0.23 refused to start. Found by the new lowest-dependencies job.
- `LibraryAlbum`'s documentation claimed the database and the album listing agree on titles.
  They agree on ids; the firmware's own folders are titled differently by each.

### Notes on firmware behaviour

- **The `my_songs` prefix is `s`**, now established rather than inferred: the library's one
  album is id 4 in `get_album_list&group=my_songs` and is keyed `s4` in the song database,
  with every other prefix accounted for.
- **Radio is a mode that takes the piano over**, and a paused position does not survive a
  visit to it. `docs/enspire-api.md` §4 has the whole account, replacing the "not
  established" note it used to carry.
- **The sequencer daemon can wedge, and only a reboot clears it.** Documented, with the
  signature, the recovery and what is and is not known about the cause, in
  `docs/enspire-api.md` §7.14 and `CONTRIBUTING.md`.

### Upgrading

For a typical consumer — Home Assistant's integration is the model case:

1. Build models by keyword, in tests especially.
2. Handle `None` from `power_status`, `quiet_status` and `playback_status`, and decide what
   `PlaybackStatus.RADIO`, `PlaybackStatus.LOAD` and `QuietMode.HEADPHONE` mean to you. For a
   media player: `RADIO` is playing, with its title in `MasterState.radio_title`.
3. If you searched radio channels, pass `include_radio=True` — and read the channel list once
   at start-up, when nothing is playing, so that it is never a search that interrupts.
4. If you distinguish "unreachable" from "unsupported", a 404 has moved from the first to the
   second.
5. Check `CurrentInfo.is_radio` before sending a transport command, and stop the radio first
   if the user has asked for something else to play.

## [0.2.3] — 2026-09-19

### Fixed

- **A case-only rename no longer deletes a song when syncing with `prune`.** The piano's
  share matches names without regard to case, so uploading `Clair de Lune.mid` overwrote
  the stored `clair de lune.mid` under its old name, and the prune then deleted that name —
  the only copy. `async_sync_directory` now matches remote paths case-insensitively, as the
  share does, so the renamed file or folder simply keeps the name the share already has.
- **`async_lookup_song` no longer re-downloads the song database for a key that stays
  missing.** A miss still refreshes once; a key the fresh database also lacks is remembered
  until the database is next read, so polling for a loaded song that was deleted from the
  share no longer fetches ~1 MB every time.
- `examples/sync_library.py` now prunes an emptied source folder under `--prune` instead of
  skipping it, ignores AppleDouble stubs when deciding a folder has nothing to sync, and
  exits non-zero when any file fails to copy rather than reporting everything current.

## [0.2.2] — 2026-09-13

### Added

- **Knowing when the library changed.** `MasterState.library_updated` reads
  `apictrl.update_window` from `master.json`, a timestamp the firmware moves when a reindex
  finishes and leaves alone for transport and volume changes. A client that already polls
  master state can tell that song listings it holds are out of date without fetching
  anything more — for instance, to refresh them in the background straight after a sync.
- **Albums from the song database.** `SongDatabase.albums` parses the database's album rows
  into `LibraryAlbum`: id, title, storage path and library, under the same ids and titles
  `async_get_albums` reports. The piano serves the whole database several times faster than
  it builds an album listing — 0.3 s against 1.8 s on a share of 304 albums — so finding a
  folder by name no longer needs the slow listing.

## [0.2.1] — 2026-08-16

### Added

- **The piano's own song database.** `async_get_song_db` fetches `/ctrl/song.json` — the
  controller UI's backing store — parsed into `SongDatabase` / `LibrarySong` and cached on
  the client. It carries what the open API's listings omit, most usefully each song's media
  format, plus genre, composer, performer and length. `async_lookup_song` joins the
  sequencer's `song_pfix`/`song_id` pair (now exposed as `MasterState.song_prefix` /
  `song_id`) against the database, refreshing the cache once on a miss so fresh recordings
  and re-indexed shares are found.
- **`SongFormat`**, the database's media-format vocabulary, with the business rule attached:
  `SongFormat.has_audio` (and `LibrarySong.has_audio`) says whether playback uses the
  speaker path — recorded backing tracks (`SMF,WAV`, `SMF,MP3`, `WAV`) and XG-scored
  accompaniment (`SMFXG`) do; `SMFSOLO` and plain `SMF` drive only the keys. This is the
  same field the controller UI uses for its format badges and for locking the tempo control
  on audio-driven songs.
- **Title search that returns candidates.** `async_search` ranks songs, playlists and radio
  channels — exact over prefix over substring over fuzzy — and returns `SearchResult` rows
  ready to hand to the matching play call. The open API's `search_title` can only play its
  single fuzzy pick; this searches the song database instead, covering every library in one
  fetch, and treats a region without DisklavierRadio as simply contributing no results.
- The song database read gets its own response ceiling, `MAX_SONG_DB_BYTES` (8 MB): the
  database is most of a megabyte on a two-thousand-song unit, which the general 1 MB
  ceiling would already reject. Every other endpoint keeps the tighter bound.

## [0.2.0] — 2026-08-16

A minor rather than a patch: the library gains a second transport and a new dependency.

### Added

- **SMB access to the piano's PC Sharing Folder.** `DisklavierShare` browses (`async_list`,
  `async_walk`, `async_stat`, `async_exists`, `async_list_shares`), transfers
  (`async_upload`, `async_upload_bytes`, `async_download`, `async_download_bytes`) and
  changes the share (`async_makedirs`, `async_rename`, `async_delete`,
  `async_remove_directory`, `async_delete_tree`) — the route for getting your own MIDI onto
  the instrument, which the HTTP API has no way to do.
- `async_sync_directory` mirrors a local tree onto the share, sending only what is missing
  or has changed, with filtering, optional pruning, a dry run, per-file progress and a
  `continue_on_error` mode for large catalogues. Verified against hardware with 1296 files.
- **`pysmb` is now a dependency** of the package rather than an optional extra. It is pure
  Python with one small dependency of its own, so requiring it costs a caller who only wants
  the HTTP API almost nothing, and it means `from aiodisklavier import DisklavierShare` works
  after a plain `pip install aiodisklavier` with no extras to remember.
- `DisklavierShareError` and its `NotFound` / `Exists` / `Auth` subclasses, carrying the
  path and the server's NT status so callers can branch on cause rather than on a message.
- `INDEXED_DEPTH_LIMIT`, `PLAYABLE_SUFFIXES`, `AUDIO_SUFFIXES`, `DEFAULT_EXCLUDES`,
  `SHARE_PC_SHARING`, `SHARE_ENSPIRE_CONTROLLER` and `SMB_PORT` are exported for callers
  building on the share.
- `examples/sync_library.py`, a runnable mirror-and-reindex script with a `--flatten` option
  for libraries with one grouping level too many.
- `docs/enspire-api.md` §8 documents the share: the SMB1-only negotiation, the guest access
  model, the NT status codes, the two-level indexing limit, and measured throughput.

### Notes

- The SMB dependency is **pysmb**, not smbprotocol. The firmware runs Samba 3.0.37, whose
  newest protocol dialect is SMB1/NT1 — Samba did not support SMB2 until 3.6 — so an SMB2
  negotiate gets the socket closed without a reply and smbprotocol, which supports SMB 2.0.2
  upwards, cannot talk to the piano at all. pysmb still speaks NT1, and negotiates SMB2 where
  a server offers it. The transport sits behind an `SMBBackend` protocol, so this choice is
  replaceable if a later firmware moves on.
- `async_sync_directory` excludes macOS AppleDouble stubs (`._*`) and `.DS_Store` by default.
  The firmware indexes an AppleDouble stub as a song in its own right, and loading one
  silently resets the piano to the first built-in song.
- **An audio file beside a MIDI file is that song's backing track**, not a song of its own.
  `song.mid` + `song.wav`/`.mp3` with matching basenames is one SMF+Audio song: the keys
  play the MIDI, the speakers play the audio, and the audio's length becomes the reported
  duration. Confirmed by adding a WAV to an indexed MIDI — the song count did not change and
  the duration moved from the MIDI's 187.6 s to the audio's 190.6 s. `PLAYABLE_SUFFIXES`
  therefore includes `.wav` and `.mp3`; narrowing a sync to `{".mid"}` produces
  transcriptions that copy, index and play with the backing track silently missing, so
  `async_sync_directory` warns when it sends a MIDI whose companion was filtered out.
- **The piano indexes only two folder levels below the share root.** A file any deeper is
  copied without complaint and never appears in the library — no error from the write, none
  from the reindex. Established by planting one file at three depths and reindexing.
  `async_sync_directory` logs a warning when it uploads past the limit; it does not
  restructure a tree, because where to fold the extra level is the caller's decision.

### Changed

- `examples/doorbell.py` sets its notification up with `DisklavierShare` rather than telling
  you to copy from the Finder, which would leave an AppleDouble stub on the share.

### Fixed

- A local file that cannot be read is reported as the `FileNotFoundError` or
  `PermissionError` it is, rather than as "Lost the SMB session" — and no longer costs a
  healthy connection a pointless reconnect on the way out. `OSError` is the net used to spot
  a dead socket, so a local read error raised inside the worker thread was landing in it.
- Listing rows whose name is not a plain filename are dropped, with a warning. SMB cannot
  express a separator inside a name, so a row carrying one is a misbehaving or hostile
  server; passed on as a `ShareEntry.path`, `dir/../../../etc/passwd` escapes the moment a
  caller writes `Path(local_dir) / entry.path`. This library's own calls were already stopped
  by the path guard on the way back, but the paths it hands out have to be safe too.
- `async_delete_tree` deletes a path that turns out to be a file, instead of walking it and
  reporting "no such path" for something plainly there.
- The progress stream reaches its total when `continue_on_error` steps over a file. A skipped
  file emitted no callback, so a bar driven by it stuck short of 100% for the rest of the run.
  Failures now report as the new `SyncAction.FAIL`.
- The `timeout` a caller passes now reaches every SMB operation, not just the handshake.
  `pysmb` gives each method its own `timeout` defaulting to 30 s, so leaving them unset made
  the constructor argument govern opening the session and nothing else — a caller asking for
  120 s to cover slow transfers quietly got 30.
- `sock_family` is passed explicitly when opening the session. It sits *between* the port and
  the timeout in `pysmb`'s `connect`, so passing three positional arguments landed the
  timeout in it, and `pysmb` then built a raw `socket.socket(<timeout>)` rather than the
  `socket.create_connection` it should. On macOS a 30 s timeout means `AF_INET6`, which
  connects to an IPv4 literal anyway and hides the mistake; on Linux 30 is not that constant
  and the connection fails outright. Passing `None` restores hostname resolution too.
- A cancelled operation abandons its session instead of leaving it for the next caller.
  `asyncio.to_thread` cannot cancel the worker, so it is still mid-request on the socket when
  the lock is released; reusing that connection put two writers on one SMB stream, which
  desynchronises it and surfaces later as a framing error on some unrelated call.
- NetBIOS-layer failures are treated as a lost session, so they get the same reconnect and
  retry as any other transport fault instead of escaping the library untranslated. `pysmb`
  has two exception trees and this is the easy one to miss: `nmb`'s `NMBError` derives
  straight from `Exception`, and `nmb` ships its own `NotConnectedError` that is a different
  class from `smb.base`'s, so neither is caught by anything aimed at the SMB layer. Found
  partway through an 865 MB transfer, where a framing error ("Invalid protocol header for
  Direct TCP session message") took down a run that `continue_on_error` should have carried
  — the collector never saw an exception it was not catching.

## [0.1.1] — 2026-08-15

### Fixed

- `async_notify` silences the piano before restoring the previous volume, so a notification
  that outlives `wait_timeout` no longer blasts its tail at the restored — usually louder —
  volume while the restore commands are in flight.
- `MasterState` and `PlaybackSnapshot` tolerate `master.json` blocks that are not objects,
  degrading to absent fields instead of raising a bare `AttributeError`.
- The doorbell example escapes device-supplied song titles before printing them, closing a
  terminal escape-sequence injection from a hostile device.

### Changed

- The README quickstart is copy-paste runnable, and a Security section states the trust
  model: plaintext, unauthenticated HTTP on a trusted LAN.
- CI, pre-commit and the contributor docs type-check `examples/` alongside the package.

### Removed

- Unused constants `PATH_DESCRIPTION`, `TEST_CHORD_SECONDS` and `PREFIX_TO_PLAYLIST_GROUP`
  (none were exported; the latter's values had no provenance). The `"s"` → `my_songs`
  prefix mapping is now commented as inferred rather than observed.

## [0.1.0] — 2026-08-15

First release. Async client for the Yamaha Disklavier ENSPIRE local HTTP API, developed
against firmware 5.24.00 on an ENSPIRE PRO grand.

### Added

- `Disklavier` client covering state, transport, volume, power, quiet mode, repeat and
  shuffle, library browsing, radio, and one-shot notifications.
- Typed models — `StaticInfo`, `CurrentInfo`, `MasterState`, `PlaybackSnapshot`, `Song`,
  `Album`, `Playlist`, `RadioChannel` — that convert the firmware's string-encoded numbers
  once, so callers never have to.
- `async_notify`, with `async_snapshot_playback` and `async_restore_playback`, for playing a
  one-shot notification and putting the piano back as it was.
- `async_play_test_chord`, which sounds a C major triad without touching the sequencer.
- Enumerations mirroring the firmware exactly: `PowerStatus`, `PlaybackStatus`, `QuietMode`,
  `SongGroup`, `PlaylistGroup`, `Genre`, `GenreSelect`, `RepeatMode`.
- PEP 561 `py.typed` marker, so type hints reach consumers.
- Browse methods translate the firmware's empty-library error envelope into the empty list
  it denotes, and `DisklavierResponseError` carries `command` and `error_info` attributes so
  the envelope errors that remain can be told apart without parsing messages.

### Security

- Response bodies are read against a 1 MiB ceiling rather than without limit, so a hostile
  or broken device cannot stream the client's host out of memory.
- Redirects are refused. No endpoint the client calls legitimately redirects, and following
  one would hand the request to whatever host a spoofed device names.

### Notes on firmware behaviour

These shaped the API and are documented in `docs/enspire-api.md`:

- Targets the versioned open API at `/api/1.0/<command>`, rather than the `/api/api.php`
  spelling of the same surface. The two were verified equivalent down to their error codes.
- There is no stop state — `stop` reports as `pause` at position zero, exposed as
  `CurrentInfo.is_stopped`.
- `power_status` has a transitional `wakeup` value lasting roughly twelve seconds, during
  which the piano ignores commands.
- Empty libraries arrive as an error envelope inside HTTP 200; the browse methods translate
  it back into an empty list.
- List responses switch between `song_list` and `item_list` depending on the group.
- State files can be read mid-rewrite and come back truncated, or carry a trailing NUL. Reads
  retry; the NUL is stripped rather than retried.
- Restoring playback stops first, because `load_song` changes the sequencer's selection
  without halting what is currently sounding.

[Unreleased]: https://github.com/reubenbijl/aiodisklavier/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/reubenbijl/aiodisklavier/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/reubenbijl/aiodisklavier/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/reubenbijl/aiodisklavier/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/reubenbijl/aiodisklavier/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/reubenbijl/aiodisklavier/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/reubenbijl/aiodisklavier/releases/tag/v0.1.0
