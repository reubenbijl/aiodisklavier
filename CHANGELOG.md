# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/reubenbijl/aiodisklavier/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/reubenbijl/aiodisklavier/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/reubenbijl/aiodisklavier/releases/tag/v0.1.0
