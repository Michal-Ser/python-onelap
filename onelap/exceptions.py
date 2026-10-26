"""Exceptions raised by the library. Everything derives from OneLapError."""


class OneLapError(Exception):
    """Base class for all errors raised by this library."""


class AuthError(OneLapError):
    """Wrong credentials. A human has to fix the account/password."""


class SessionExpired(OneLapError):
    """The session is not accepted and could not (or may not) be renewed."""


class RateLimited(OneLapError):
    """HTTP 429 persisted after all retries. Try again later."""


class NetworkError(OneLapError):
    """Transport failure (DNS, connection, timeout) after all retries."""


class ApiError(OneLapError):
    """Unexpected status or response shape: the API probably changed."""

    def __init__(self, message, status=None, snippet=""):
        super().__init__(message)
        self.status = status
        self.snippet = snippet

    def __str__(self):
        base = super().__str__()
        if self.status is not None:
            base += " (status %s)" % self.status
        if self.snippet:
            base += ": %s" % self.snippet
        return base


class NotFitError(OneLapError):
    """A downloaded file is not a FIT file."""
