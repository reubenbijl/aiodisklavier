# Contributing

Reports from other ENSPIRE models, regions and firmware versions are especially useful — this library was developed against a single piano, an ENSPIRE PRO grand on 5.24.00, and almost every quirk it works around was found on that one instrument.

The quickest useful contribution needs no checkout at all: run `python -m aiodisklavier <ip>`
against your piano and paste what it prints into a
[piano report](https://github.com/reubenbijl/aiodisklavier/issues/new/choose). It is read-only
and silent, and it reports the shape of the piano's state rather than its contents.

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test,lint]"
```

## Before opening a pull request

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy aiodisklavier examples
.venv/bin/pytest --cov
```

CI runs exactly these, across every Python version the package claims to support.

## The two rules worth knowing

**Coverage is gated at 100%, including branches.** The library is small and entirely
reachable from tests, and keeping it there is cheap. If something genuinely cannot be
exercised, restructure it rather than adding a pragma — that is how the JSON retry loop ended up shaped the way it is.

**Tests run against a real HTTP server**, not a mocking layer. `tests/conftest.py` starts an
`aiohttp` test server that imitates the piano. This means assertions about query strings are
checking what actually went over the wire, which matters because the firmware's valueless flag
arguments (`?set_power_status&sleep`) are easy to encode wrongly. It also keeps the suite from
breaking every time `aiohttp` changes its internals — an earlier version used `aioresponses`
and broke on exactly that.

## Adding support for something new

1. Check `docs/enspire-api.md` first. It documents the whole surface with provenance marked
   per claim, including endpoints this library does not yet wrap.
2. Mark new findings the same way — `[live]` for observed by calling a piano, `[api-test]` for
   the piano's own `/ctrl/api_test.html`, `[app]` for its web app's JavaScript, `[inferred]`
   for anything deduced. Everything in that document is reproducible against a stock piano
   with `curl` and a browser; please keep it that way rather than citing sources a reader
   cannot check.
3. If a claim cannot be established that way, put it in the "Not established" section instead
   of asserting it.
4. When hardware does something the interface does not explain, say so in the test. Several
   tests here look arbitrary without their comment; the comment is the point.

## Testing against a real piano

The unit suite's fake can only repeat what one piano once did. It cannot notice the firmware
changing, and it has been wrong in exactly the way a fake can be: for two releases the
restore sent its seek while the song was still loading, the piano dropped it, and every test
passed — because every test checked that the request went out. `tests/hardware/` runs the
same library against a real instrument, and is how each release is checked.

It is opt-in and has three levels. Run it without `--cov`; the coverage gate belongs to the
unit suite.

```bash
DISKLAVIER_HOST=192.168.1.50 .venv/bin/pytest -m hardware                                # read
DISKLAVIER_HOST=192.168.1.50 DISKLAVIER_HARDWARE=changes .venv/bin/pytest -m hardware
DISKLAVIER_HOST=192.168.1.50 DISKLAVIER_HARDWARE=sequencer .venv/bin/pytest -m hardware
```

- **`read`**, the default, only reads. It is silent, changes nothing, and is safe to run while
  the piano is playing. A failure here is usually news rather than a bug: your piano said
  something this library has no name for. Please report it.
- **`changes`** also alters settings and puts them back: the volume, the repeat mode, a
  scratch file on the share that is never indexed. Silent, and nothing touches the sequencer.
- **`sequencer`** also loads, seeks, plays a few seconds of a built-in song at low volume, and
  starts and stops a radio channel. **It makes sound, and it carries a real risk** — read the
  next section before turning it on.

## The sequencer can wedge

This is the one way to make a real mess. The piano's sequencer daemon can get into a state
where it goes on answering, goes on accepting `load_song` — the selection and the title
change — and never finishes loading anything. `current_info` reports a `song_length` of `0`,
seeks and `play` are silent no-ops, and nothing will play, from this library, the phone app
or the panel. Neither a reindex nor a standby cycle recovers it. A reboot does:

```bash
curl "http://$PIANO/ctrl/setRcs.php?status=reboot"   # back in ~30 s; leave it 30 s more
```

It has happened four times on the reference piano, twice during one morning of the testing
that produced this section. What sets it off is not established. The suspect is commands
arriving on top of each other: both of that morning's incidents came while `stop` and
`load_song` were being sent a few tens of milliseconds apart, the second one minutes after a
clean reboot with nothing else going on, and the same commands sent a second apart — all
day, and through the whole hardware suite afterwards — never did it.

So, when you experiment:

- **Pace everything.** Leave a second or more between commands to the sequencer. A script
  that fires `stop`, `load_song` and a seek back to back is the shape that went wrong.
- **Do not send a loading sequencer anything.** `load_song` answers at once and the load
  takes about two seconds; what arrives meanwhile is dropped at best.
- **Do not retry into trouble.** A command the piano has ignored twice will not be obeyed a
  third time, and a version of the restore that tried four times was running when it wedged.
- **Watch for the signature and stop.** A loaded song with length `0`, or `load` that does not
  clear within ten seconds, means stop sending commands. The hardware suite's `healthy`
  fixture checks for exactly this and ends the run.
- **Snapshot state first, prefer reads**, and remember that `putNoteOn.php`, anything that
  starts playback, and the radio all make noise at whatever volume the piano is set to.
- **`get_radio_channel_list` is not a read.** It stops whatever is playing.

If you do wedge it, capture `master.json` and `current_info` and a note of what you sent
before rebooting. The pattern across incidents is the only route to the cause.
