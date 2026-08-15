#!/usr/bin/env python3
"""Use the Disklavier as a doorbell: play a short MIDI file, then resume what was playing.

The piano becomes its own notification chime. A MIDI file lives in the PC Sharing Folder;
``async_notify`` snapshots the current song and position, plays the notification once, waits
for it to finish, and restores playback -- volume included.

One-time setup
--------------
1. Mount the piano's SMB share and drop a ``.mid`` into it. The share is guest-accessible::

       # macOS
       open "smb://<piano-ip>/PC%20Sharing%20Folder"
       cp doorbell.mid "/Volumes/PC Sharing Folder/"

   ``make_doorbell.py`` alongside this file generates a two-note "ding-dong" if you need one.

2. Reindex so the piano sees the new file (``async_refresh_library`` below, or the web UI's
   Songs -> PC Sharing -> DB Reload). The rescan reassigns song ids, which is why this
   script resolves the notification by *title* rather than a hard-coded id.

Usage
-----
    python examples/doorbell.py <piano-ip> [title]
    python examples/doorbell.py <piano-ip> --chord      # fixed C-major triad, no file needed
    python examples/doorbell.py <piano-ip> doorbell --volume 90

The ``--chord`` mode uses ``async_play_test_chord``: it addresses the MIDI daemon directly,
so it sounds instantly and never touches the sequencer -- handy when nothing has been set up
on the share yet, at the cost of a fixed chord you cannot change.
"""

from __future__ import annotations

import argparse
import asyncio

import aiohttp

from aiodisklavier import Disklavier, DisklavierResponseError, Song, SongGroup


async def _find_by_title(piano: Disklavier, title: str, group: SongGroup) -> Song:
    """Resolve a song title to its current id, tolerant of case."""
    songs = await piano.async_get_songs(group)
    folded = title.casefold()
    for song in songs:
        if song.title.casefold() == folded:
            return song
    available = ", ".join(s.title for s in songs) or "(library empty)"
    raise SystemExit(
        f"No song titled {title!r} in {group.value}.\n"
        f"Drop the file onto the share and reindex first. Present: {available}"
    )


async def ring_doorbell(
    host: str, title: str, group: SongGroup, volume: int | None
) -> None:
    """Play a notification file and restore whatever was playing."""
    async with aiohttp.ClientSession() as session:
        piano = Disklavier(host, session)

        # Pick up any files added to the share since the last scan, then resolve the id.
        await piano.async_refresh_library()
        song = await _find_by_title(piano, title, group)

        before = await piano.async_get_current_info()
        print(f"before: {before.song_title!r} ({before.playback_status.value})")
        print(f"ringing: {song.title!r} (id {song.song_id})...")

        await piano.async_notify(song_id=song.song_id, group=group, volume=volume)

        # current_info trails a reselect by a beat, so give it a moment before reading back.
        await asyncio.sleep(1.5)
        after = await piano.async_get_current_info()
        print(f"after:  {after.song_title!r} ({after.playback_status.value})")


async def test_chord(host: str) -> None:
    """Fire the built-in C-major triad -- no file, no sequencer, no restore needed."""
    async with aiohttp.ClientSession() as session:
        piano = Disklavier(host, session)
        print("playing test chord...")
        await piano.async_play_test_chord()
        print("done")


def main() -> None:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Piano hostname or IP address")
    parser.add_argument(
        "title",
        nargs="?",
        default="doorbell",
        help="Title of the notification file in the PC Sharing Folder (default: doorbell)",
    )
    parser.add_argument(
        "--chord",
        action="store_true",
        help="Play the fixed test chord instead of a file",
    )
    parser.add_argument(
        "--volume",
        type=int,
        default=None,
        help="Play at this volume (0-100), restoring the previous one afterwards",
    )
    args = parser.parse_args()

    try:
        if args.chord:
            asyncio.run(test_chord(args.host))
        else:
            asyncio.run(
                ring_doorbell(
                    args.host, args.title, SongGroup.PC_SHARING_FOLDER, args.volume
                )
            )
    except DisklavierResponseError as err:
        raise SystemExit(f"Piano returned an error: {err}") from err


if __name__ == "__main__":
    main()
