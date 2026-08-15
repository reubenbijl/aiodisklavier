# Contributing

Reports from other ENSPIRE models, regions and firmware versions are especially useful — this library was developed against a single piano, an ENSPIRE PRO grand on 5.24.00, and almost every quirk it works around was found on that one instrument.

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

## A caution about testing against a real piano

The sequencer can be wedged by commands issued in the wrong order — this happened during
development, and neither reindexing nor a standby cycle recovered it. Snapshot state before
you experiment, prefer read-only calls, and remember that `putNoteOn.php` and anything that
starts playback will make noise at whatever volume the piano is currently set to.
