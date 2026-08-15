"""Exceptions raised by :mod:`aiodisklavier`."""

from __future__ import annotations


class DisklavierError(Exception):
    """Base class for every error raised by this library."""


class DisklavierConnectionError(DisklavierError):
    """The piano could not be reached, or it timed out."""


class DisklavierCommandError(DisklavierError):
    """The piano rejected the request.

    Raised for HTTP 400, which the firmware returns for an unknown command, an unknown
    ``group``, or an out-of-range or non-numeric argument.
    """


class DisklavierResponseError(DisklavierError):
    """The piano returned a response that could not be understood.

    Also raised when a read succeeds at the HTTP level but carries ``"status": "error"`` in
    the JSON envelope, which the firmware does for cases such as an empty library.
    """
