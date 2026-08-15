"""Generate a two-note 'ding-dong' doorbell as a Standard MIDI File (format 0).

No dependencies. The classic doorbell is a descending major third -- a bright 'ding'
followed by a lower, longer 'dong'. Here: E5 then C5, on MIDI channel 1 so the Disklavier
plays it on the piano.
"""

from __future__ import annotations

import struct
from pathlib import Path

DIVISION = 480  # ticks per quarter note
TEMPO = 500_000  # microseconds per quarter -> 120 BPM, so 480 ticks = 0.5 s

DING = 76  # E5
DONG = 72  # C5
VELOCITY = 100


def _vlq(value: int) -> bytes:
    """Encode an int as a MIDI variable-length quantity."""
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _event(delta: int, *data: int) -> bytes:
    return _vlq(delta) + bytes(data)


def build() -> bytes:
    """Build the SMF byte stream."""
    track = bytearray()
    # Tempo meta event.
    track += _vlq(0) + b"\xff\x51\x03" + struct.pack(">I", TEMPO)[1:]

    # ding: E5 for half a beat.
    track += _event(0, 0x90, DING, VELOCITY)  # note on
    track += _event(DIVISION, 0x80, DING, 0)  # note off after 0.5 s

    # dong: C5, held two beats, starting as the ding releases.
    track += _event(0, 0x90, DONG, VELOCITY)
    track += _event(DIVISION * 2, 0x80, DONG, 0)  # off after 1.0 s

    # End of track.
    track += _vlq(0) + b"\xff\x2f\x00"

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, DIVISION)
    chunk = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    return header + chunk


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "doorbell.mid"
    with Path(path).open("wb") as f:
        f.write(build())
    print(f"wrote {path}")
