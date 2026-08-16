#!/usr/bin/env python3
"""Mirror a local MIDI library onto the piano's PC Sharing Folder, then reindex.

Only what is missing or has changed is transferred, so this is cheap to re-run and an
interrupted run picks up where it stopped. Re-scanning a catalogue of ~1300 files that is
already current takes under a second.

Backing tracks come too
-----------------------
An audio file sharing a MIDI file's basename -- ``song.mid`` and ``song.wav`` -- is that
song's backing track, and the piano plays them together. This script syncs with
``PLAYABLE_SUFFIXES``, which includes audio, so those pairs stay together. Filtering to MIDI
alone would leave every transcription playing as a bare piano part, and nothing would say so.

Mind the folder depth
---------------------
The piano's indexer descends exactly two folder levels below the share root::

    <destination>/<Composer>/song.mid      indexed, <Composer> becomes an album
    <destination>/maestro/<Composer>/x.mid copied, and invisible to the piano

Nothing reports the second case -- not the copy, not the reindex -- so if your library has an
extra grouping level, sync each of its subdirectories separately, as ``--flatten`` does here.

Usage
-----
    python examples/sync_library.py <piano-ip> ~/Music/disklavier
    python examples/sync_library.py <piano-ip> ~/Music/disklavier --dest ImpromptuApp
    python examples/sync_library.py <piano-ip> ~/catalogue --flatten maestro --dry-run
    python examples/sync_library.py <piano-ip> ~/Music/disklavier --prune
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

import aiohttp

from aiodisklavier import (
    PLAYABLE_SUFFIXES,
    Disklavier,
    DisklavierError,
    DisklavierShare,
    SyncAction,
    SyncProgress,
    SyncResult,
)


def parse_args() -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="the piano's hostname or IP address")
    parser.add_argument("source", type=Path, help="local directory to mirror")
    parser.add_argument(
        "--dest",
        default="",
        help="destination folder on the share (default: the share root)",
    )
    parser.add_argument(
        "--flatten",
        metavar="DIR",
        action="append",
        default=[],
        help=(
            "a subdirectory of SOURCE whose contents go straight into DEST, dropping its "
            "own name. Use for a grouping level that would push files past the piano's "
            "two-folder indexing limit. Repeatable."
        ),
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete anything on the share the local tree does not have",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen, change nothing",
    )
    parser.add_argument(
        "--no-reindex",
        action="store_true",
        help="skip the reindex; nothing transferred will be playable until you do one",
    )
    return parser.parse_args()


def make_reporter() -> Callable[[SyncProgress], None]:
    """Build a progress callback that prints a line per directory and every 50th file."""
    counts = {action.value: 0 for action in SyncAction}
    started = time.monotonic()

    def report(step: SyncProgress) -> None:
        counts[step.action.value] += 1
        if step.action is SyncAction.CREATE_DIRECTORY:
            print(f"  [{step.index:>5}/{step.total}] mkdir {step.path}")
        elif step.action is SyncAction.UPLOAD and counts["upload"] % 50 == 0:
            elapsed = time.monotonic() - started
            print(
                f"  [{step.index:>5}/{step.total}] {counts['upload']} sent, "
                f"{elapsed:5.1f}s, last: {step.path}"
            )

    return report


def has_playable(directory: Path) -> bool:
    """Whether a directory holds anything the piano could play.

    Checked before syncing so that a source directory of build scripts or notes does not
    leave an empty folder behind on the share.
    """
    return any(
        path.suffix.casefold() in PLAYABLE_SUFFIXES
        for path in directory.rglob("*")
        if path.is_file()
    )


def summarise(label: str, result: SyncResult) -> None:
    """Print one sync's outcome."""
    print(
        f"{label}: {len(result.uploaded)} uploaded "
        f"({result.bytes_uploaded / 1e6:.1f} MB), {len(result.directories)} folders, "
        f"{len(result.skipped)} already current, {len(result.removed)} removed, "
        f"{len(result.failed)} failed"
    )
    for failure in result.failed:
        print(f"  FAILED {failure.path}: {failure.error}")


async def main() -> int:
    """Mirror the library and reindex."""
    args = parse_args()
    # The library warns through the logging module when files land deeper than the piano
    # will index; without this the warning would go nowhere.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if not args.source.is_dir():
        print(f"not a directory: {args.source}", file=sys.stderr)
        return 2

    # Each entry is (local directory, destination on the share). A --flatten subdirectory
    # keeps its contents but loses its own name, which is what keeps the tree within the
    # piano's two-level indexing limit.
    jobs = [(args.source / name, args.dest) for name in args.flatten]
    if not jobs:
        jobs = [(args.source, args.dest)]
    else:
        jobs += [
            (entry, f"{args.dest}/{entry.name}" if args.dest else entry.name)
            for entry in sorted(args.source.iterdir())
            if entry.is_dir() and entry.name not in args.flatten
        ]

    report = make_reporter()
    changed = False
    try:
        async with DisklavierShare(args.host) as share:
            for source, destination in jobs:
                if not source.is_dir():
                    print(f"skipping missing {source}", file=sys.stderr)
                    continue
                if not has_playable(source):
                    print(f"skipping {source}: nothing playable in it")
                    continue
                print(f"\n{source} -> {destination or '<share root>'}")
                result = await share.async_sync_directory(
                    source,
                    destination,
                    suffixes=PLAYABLE_SUFFIXES,
                    prune=args.prune,
                    dry_run=args.dry_run,
                    continue_on_error=True,
                    progress=None if args.dry_run else report,
                )
                summarise("  " + source.name, result)
                changed = changed or result.changed

        if args.dry_run:
            print("\nDry run: nothing was changed.")
        elif not changed:
            print("\nEverything was already current.")
        elif args.no_reindex:
            print("\nSkipped the reindex; the piano will not list the new files yet.")
        else:
            print("\nReindexing...")
            async with aiohttp.ClientSession() as session:
                await Disklavier(args.host, session).async_refresh_library()
            print(
                "Reindex issued. It runs in the background and reassigns song ids, so"
            )
            print("resolve songs by title rather than reusing an id from before.")
    except DisklavierError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
