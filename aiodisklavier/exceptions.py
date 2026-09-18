"""Exceptions raised by :mod:`aiodisklavier`.

The split callers most often want is "will asking again help?". A
:class:`DisklavierConnectionError` is worth a retry; everything else is the piano's answer,
and the same request will get the same one.
"""

from __future__ import annotations


class DisklavierError(Exception):
    """Base class for every error raised by this library."""


class DisklavierConnectionError(DisklavierError):
    """The piano could not be reached, or it timed out.

    Also raised for an HTTP 5xx. The piano's web server answers 5xx when the PHP behind it
    or the control daemon behind that has fallen over, which is the HTTP layer failing
    rather than the piano answering -- transient, and worth the same retry as a dropped
    connection.
    """


class DisklavierCommandError(DisklavierError):
    """The piano rejected the request.

    Raised for HTTP 400, which the firmware returns for an unknown command, an unknown
    ``group``, or an out-of-range or non-numeric argument.
    """


class DisklavierResponseError(DisklavierError):
    """The piano answered, but not with something this library can use.

    Malformed JSON that outlasted its retries, a body past the size ceiling, a redirect,
    or an HTTP 4xx other than the 400 that :class:`DisklavierCommandError` covers. A 404
    lands here rather than in :class:`DisklavierConnectionError` deliberately: it means
    this piano does not serve that endpoint -- a different model or firmware, most likely
    -- and asking again will not change that, so it must not look like an outage.

    A command the piano understood and declined raises the
    :class:`DisklavierEnvelopeError` subclass instead.

    :ivar http_status: The HTTP status, when the status itself was the problem. ``None``
        otherwise.
    :ivar command: The open API command whose envelope failed. Only ever set on a
        :class:`DisklavierEnvelopeError`; kept here so code written against earlier
        releases, which read it off this class, keeps working.
    :ivar error_info: The firmware's ``error_info`` string, likewise.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        error_info: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Initialise the error, optionally with the details that caused it."""
        super().__init__(message)
        self.command = command
        self.error_info = error_info
        self.http_status = http_status


class DisklavierEnvelopeError(DisklavierResponseError):
    """The piano understood a command and answered that it could not serve it.

    The open API reports this inside HTTP 200, as ``"status": "error"`` with the reason in
    ``error_info`` -- ``"no subscription"`` from ``play_radio`` for a channel the account
    does not include, for instance. It is the piano's considered answer rather than a
    garbled one, which is why it is its own class: catching this leaves a truncated or
    oversized response to surface as the fault it is.

    An empty library arrives the same way (``error_info: "no song"``), but the browse
    methods translate that envelope into an empty list rather than raising.

    :ivar command: The open API command whose envelope failed.
    :ivar error_info: The firmware's ``error_info`` string, when the envelope carried one.
    """

    def __init__(self, message: str, *, command: str, error_info: str | None) -> None:
        """Initialise the error with the envelope fields that caused it."""
        super().__init__(message, command=command, error_info=error_info)


class DisklavierShareError(DisklavierError):
    """An operation on the piano's SMB share failed.

    Raised when the server answered and refused: a missing path, a name collision, a
    directory that is not empty. Losing the connection instead raises
    :class:`DisklavierConnectionError`, the same as it does for the HTTP API.

    :ivar path: The share-relative path the operation was for, when it had one.
    :ivar status: The NT status code the server returned, when there was one. Useful for
        distinguishing causes this library has not given a subclass to.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        """Initialise the error, optionally with the path and status that caused it."""
        super().__init__(message)
        self.path = path
        self.status = status


class DisklavierShareNotFoundError(DisklavierShareError):
    """The share has no such file or directory."""


class DisklavierShareExistsError(DisklavierShareError):
    """Something is already at that path on the share."""


class DisklavierShareAuthError(DisklavierShareError):
    """The share refused the credentials, or refused the operation.

    Stock firmware serves the PC Sharing Folder to guests, so this normally means a piano
    that has been given a password, or a write attempted against the read-only
    ``ENSPIRE Controller`` share.
    """
