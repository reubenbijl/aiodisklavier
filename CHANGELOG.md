# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
