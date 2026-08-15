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
    the JSON envelope, which the firmware does when a command cannot be served -- for
    example when the DisklavierRadio service is unavailable. (An empty library arrives the
    same way, but the browse methods translate that envelope into an empty list rather
    than raising.)

    :ivar command: The open API command whose envelope failed, when the error came from a
        command envelope. ``None`` otherwise.
    :ivar error_info: The firmware's ``error_info`` string, when the envelope carried one.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        error_info: str | None = None,
    ) -> None:
        """Initialise the error, optionally with the envelope fields that caused it."""
        super().__init__(message)
        self.command = command
        self.error_info = error_info
